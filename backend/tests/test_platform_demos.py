from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

from shovel.automation.domain import (
    EventTrigger,
    ManualTrigger,
    NormalizedEvent,
    ProactiveConfiguration,
    QuietHours,
    ScheduleDefinition,
    ScheduleTrigger,
    TargetCapability,
)
from shovel.capability.domain import (
    CapabilityContent,
    CapabilityKind,
    CapabilityVersion,
    Condition,
    DependencyLock,
    FunctionContract,
    FunctionImplementation,
    InputBinding,
    Permission,
    RiskProfile,
    SideEffectProfile,
    SkillImplementation,
    WorkflowEdge,
    WorkflowImplementation,
    WorkflowNode,
)
from shovel.capability.registry import CapabilityRegistry
from shovel.cli.app import app as cli
from shovel.db.database import create_async_database_engine, create_async_session_factory
from shovel.execution.domain import Execution, ExecutionState, ExecutorResult
from shovel.execution.executors import FunctionExecutor
from shovel.execution.ports import ExecutorContext
from shovel.platform.agent_tools import agent_tools
from shovel.platform.domain import PlatformCommand
from shovel.runtime.api import configure_api
from shovel.runtime.client import _decode
from shovel.runtime.platform_client import _perform
from shovel.runtime.service import Runtime, create_runtime
from shovel.security.domain import ApprovalPreference, UserSecurityPolicy
from shovel.services.resource_service import ResourceService
from test_capability_registry import locked
from test_execution_engine import reference
from test_hosted_runtime import settings_at

READ = Permission(action="read", resource="demo-documents")
EXTRA = Permission(action="read", resource="demo-unrelated")
SEND = Permission(action="send_demo", resource="demo-mailbox")
GRANTS = (READ, EXTRA, SEND)
TEXT = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
MAIL = {
    "type": "object",
    "properties": {"draft": {"type": "string"}},
    "required": ["draft"],
    "additionalProperties": False,
}
RECEIPT = {
    "type": "object",
    "properties": {"demo_only": {"type": "boolean"}, "sent": {"type": "boolean"}},
    "required": ["demo_only", "sent"],
    "additionalProperties": False,
}
SEND_CONTRACT = FunctionContract(
    implementation_id="demo.send_email",
    input_schema=MAIL,
    output_schema=RECEIPT,
    required_permissions=(SEND,),
    risk_profile=RiskProfile(level="high", reasons=("Demo external-write gate verification",)),
    side_effect_profile=SideEffectProfile(kind="external_write"),
)


@dataclass
class Model:
    requests: list[dict[str, Any]] = field(default_factory=list)
    receipts: list[str] = field(default_factory=list)
    blocked_instruction: str | None = None
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def respond(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "model.invalid"
        body = json.loads(request.content)
        self.requests.append(body)
        assert not body.get("tools")
        instructions = "\n".join(
            message["content"] for message in body["messages"] if message["role"] in {"system", "developer"}
        )
        if self.blocked_instruction and self.blocked_instruction in instructions:
            self.entered.set()
            await self.release.wait()
        answers = {
            "Daily Briefing": "Today's deterministic briefing",
            "Analyze Email": "High-importance email analyzed",
            "Generate Draft": "Draft response, not sent",
            "Analyze Customer Incident V1": "Incident analysis V1",
            "Analyze Customer Incident V2": "Incident analysis V2",
        }
        answer = next((value for key, value in answers.items() if key in instructions), None)
        assert answer is not None, "Unexpected Skill instructions"
        return httpx.Response(
            200, json={"created": 1, "choices": [{"message": {"role": "assistant", "content": answer}}]}
        )

    async def send_demo(self, context: ExecutorContext) -> ExecutorResult:
        assert context.execution.input == {"draft": "Draft response, not sent"}
        self.receipts.append(context.execution.id)
        return ExecutorResult(output={"demo_only": True, "sent": False})


@dataclass
class Demo:
    runtime: Runtime
    api: httpx.AsyncClient
    model: Model
    now: list[float]

    async def command(self, command: PlatformCommand, *, confirm: bool = False) -> dict[str, Any]:
        if confirm:
            preview = await self.command(command.model_copy(update={"prepare": True}))
            command = command.model_copy(update={"confirmation_id": preview["confirmation_id"]})
        response = await self.api.post("/api/v1/platform/commands", json=command.model_dump(mode="json"))
        assert response.is_success, response.text
        return response.json()

    async def publish(self, name: str, content: CapabilityContent) -> CapabilityVersion:
        draft = await self.command(
            PlatformCommand(
                action="capability.create",
                name=name,
                kind=CapabilityKind(content.implementation.kind),
                content=content,
            )
        )
        return CapabilityVersion.model_validate(
            await self.command(
                PlatformCommand(
                    action="capability.publish",
                    identifier=draft["capability_id"],
                    version=1,
                    expected_revision=1,
                ),
                confirm=True,
            )
        )

    async def service(self, configuration: ProactiveConfiguration) -> dict[str, Any]:
        draft = await self.command(
            PlatformCommand(action="proactive.create", name="Demo service", configuration=configuration)
        )
        return await self.command(
            PlatformCommand(action="proactive.enable", identifier=draft["id"], expected_revision=1),
            confirm=True,
        )

    async def execution(self, identifier: str) -> Execution:
        return await self.runtime.execution_engine.repository.get(self.runtime.platform.scope, identifier)

    async def history(self, service_id: str) -> list[dict[str, Any]]:
        return (await self.api.get(f"/api/v1/platform/proactive/{service_id}/history")).json()["items"]

    async def pump(self, identifier: str) -> Execution:
        for _ in range(24):
            await self.runtime.wakeups.run_due()
            progressed = await self.runtime.execution_engine.run_once()
            current = await self.execution(identifier)
            if current.state.terminal or not progressed:
                return current
        pytest.fail("Demo exceeded its bounded execution driver")


@pytest.fixture
async def demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Demo]:
    model = Model()
    monkeypatch.setattr(
        "shovel.runtime.service.CapabilityRegistry",
        lambda repository: CapabilityRegistry(repository, functions=(SEND_CONTRACT,)),
    )
    monkeypatch.setattr(
        "shovel.runtime.service.FunctionExecutor",
        lambda _: FunctionExecutor({"demo.send_email": model.send_demo}),
    )
    settings = settings_at(tmp_path)
    settings.platform.security = UserSecurityPolicy(
        user_grants=GRANTS, platform_constraints=GRANTS, allowed_side_effects=("none", "external_write")
    )
    settings.platform.agent_permissions = GRANTS
    database = create_async_database_engine(settings.database.url)
    engine = await anext(database)
    sessions = create_async_session_factory(engine)
    managed = create_runtime(settings, engine, sessions, ResourceService(sessions))
    try:
        runtime = await anext(managed)
        runtime.chat.agent._client = httpx.AsyncClient(transport=httpx.MockTransport(model.respond))
        now = [datetime(2030, 1, 1, 8, 59, 59, tzinfo=UTC).timestamp()]
        runtime.proactive.clock = runtime.proactive.repository.clock = lambda: now[0]
        app = FastAPI()
        configure_api(app, lambda: runtime, settings)
        await runtime.events.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9999"
        ) as api:
            yield Demo(runtime, api, model, now)
    finally:
        model.release.set()
        await managed.aclose()
        await database.aclose()


def binding(*path: str) -> InputBinding:
    return InputBinding(source="context", path=path)


def skill(instructions: str) -> CapabilityContent:
    return CapabilityContent(
        implementation=SkillImplementation(instructions=instructions),
        input_schema=TEXT,
        output_schema=TEXT,
        required_permissions=(READ,),
        permission_boundary=(READ,),
    )


def call(identifier: str, version: CapabilityVersion, *source: str) -> WorkflowNode:
    return WorkflowNode(
        id=identifier,
        kind=version.content.implementation.kind,
        capability=reference(version),
        input_mapping={"text": binding(*source)},
    )


def workflow(
    *nodes: WorkflowNode,
    dependencies: tuple[CapabilityVersion, ...],
    output: dict[str, InputBinding],
    external: bool = False,
) -> CapabilityContent:
    graph = (
        WorkflowNode(id="start", kind="input"),
        *nodes,
        WorkflowNode(id="end", kind="output", input_mapping=output),
    )
    return CapabilityContent(
        implementation=WorkflowImplementation(
            nodes=graph,
            edges=tuple(WorkflowEdge(source=left.id, target=right.id) for left, right in pairwise(graph)),
        ),
        input_schema=TEXT,
        output_schema=RECEIPT if external else TEXT,
        required_permissions=(READ, SEND) if external else (READ,),
        permission_boundary=GRANTS,
        allowed_capabilities=tuple(reference(version) for version in dependencies),
        dependency_lock=DependencyLock(dependencies=tuple(locked(version) for version in dependencies)),
        risk_profile=RiskProfile(level="high" if external else "low"),
        side_effect_profile=SideEffectProfile(kind="external_write" if external else "none"),
    )


def configuration(
    version: CapabilityVersion, *, latest: bool = False, **kwargs: Any
) -> ProactiveConfiguration:
    return ProactiveConfiguration(
        trigger=kwargs.pop("trigger", ManualTrigger()),
        target=TargetCapability(
            capability_id=version.capability_id,
            version_policy="latest_published" if latest else "exact",
            version=None if latest else version.number,
        ),
        input_mapping=kwargs.pop(
            "input_mapping", {"text": InputBinding(source="literal", value="Demo input")}
        ),
        permission_boundary=(READ, SEND)
        if version.content.side_effect_profile.kind == "external_write"
        else (READ,),
        **kwargs,
    )


async def test_demo_1_daily_briefing_manual_and_schedule_share_workflow(demo: Demo) -> None:
    child = await demo.publish("Briefing Skill", skill("Daily Briefing"))
    version = await demo.publish(
        "Daily Briefing",
        workflow(
            call("briefing", child, "input", "text"),
            dependencies=(child,),
            output={"text": binding("nodes", "briefing", "text")},
        ),
    )
    tools = {tool.__name__: tool for tool in agent_tools(demo.runtime.platform)}
    manual = await tools["invoke_capability"](
        version.capability_id, 1, "daily-agent", '{"text":"Demo input"}'
    )
    scheduled = await demo.service(
        configuration(
            version,
            trigger=ScheduleTrigger(schedule=ScheduleDefinition(kind="daily", hour=9, minute=0)),
        )
    )
    demo.now[0] += 1
    await demo.runtime.schedules.tick(demo.now[0])
    await demo.runtime.events.join()
    rows = await demo.history(scheduled["id"])
    assert len(rows) == 1
    manual_result = await demo.pump(manual["id"])
    scheduled_result = await demo.pump(rows[0]["id"])
    assert manual_result.state == scheduled_result.state == ExecutionState.SUCCEEDED
    assert manual_result.output == scheduled_result.output == {"text": "Today's deterministic briefing"}
    assert manual_result.snapshot.capability == scheduled_result.snapshot.capability
    assert manual_result.snapshot.capability.content.dependency_lock == version.content.dependency_lock
    assert manual_result.snapshot.context.source == "agent"
    assert scheduled_result.snapshot.context.proactive_snapshot["revision"] == scheduled["revision"]
    await demo.runtime.schedules.tick(demo.now[0])
    await demo.runtime.events.join()
    assert len([row for row in await demo.history(scheduled["id"]) if not row["parent_execution_id"]]) == 1


async def test_demo_2_email_gates_explicit_approval_and_mock_send(demo: Demo) -> None:
    analyze = await demo.publish("Analyze Email", skill("Analyze Email"))
    draft = await demo.publish("Generate Draft", skill("Generate Draft"))
    send = await demo.publish(
        "Demo Send Email",
        CapabilityContent(
            implementation=FunctionImplementation(implementation_id=SEND_CONTRACT.implementation_id),
            input_schema=MAIL,
            output_schema=RECEIPT,
            required_permissions=(SEND,),
            permission_boundary=(SEND,),
            risk_profile=SEND_CONTRACT.risk_profile,
            side_effect_profile=SEND_CONTRACT.side_effect_profile,
        ),
    )
    version = await demo.publish(
        "Important Email Assistant",
        workflow(
            call("analyze", analyze, "input", "text"),
            call("draft", draft, "nodes", "analyze", "text"),
            WorkflowNode(id="approval", kind="approval"),
            WorkflowNode(
                id="send",
                kind="function",
                capability=reference(send),
                input_mapping={"draft": binding("nodes", "draft", "text")},
            ),
            dependencies=(analyze, draft, send),
            output={
                "demo_only": binding("nodes", "send", "demo_only"),
                "sent": binding("nodes", "send", "sent"),
            },
            external=True,
        ),
    )
    service = await demo.service(
        configuration(
            version,
            trigger=EventTrigger(event_type="email.received"),
            conditions=(Condition(path=("payload", "importance"), operator="eq", value="high"),),
            input_mapping={"text": InputBinding(source="event", path=("payload", "body"))},
        )
    )
    event = NormalizedEvent(
        id="important-email",
        scope=demo.runtime.platform.scope,
        event_type="email.received",
        source="demo-mail",
        occurred_at=demo.now[0],
        payload={"importance": "normal", "body": "Please review this demo incident."},
    )
    await demo.runtime.events.publish(event)
    await demo.runtime.events.join()
    assert await demo.history(service["id"]) == []
    await demo.runtime.events.publish(
        event.model_copy(update={"payload": {**event.payload, "importance": "high"}})
    )
    await demo.runtime.events.join()
    execution_id = (await demo.history(service["id"]))[0]["id"]
    reasons: set[str] = set()
    rechecked = False
    for _ in range(12):
        current = await demo.pump(execution_id)
        if current.state.terminal:
            break
        pending = [
            row
            for row in (await demo.api.get("/api/v1/platform/approvals")).json()["items"]
            if row["state"] == "pending"
        ]
        assert pending, current
        for approval in pending:
            waiting = await demo.execution(approval["execution_id"])
            assert waiting.wait_reason == "waiting_for_approval"
            command = PlatformCommand(
                action="approval.approve",
                identifier=approval["id"],
                execution_id=approval["execution_id"],
            )
            reasons.update(approval["reasons"])
            if not rechecked and waiting.snapshot.capability.kind == CapabilityKind.FUNCTION:
                assert len(demo.model.requests) == 2
                assert "Please review this demo incident." in json.dumps(demo.model.requests[0])
                assert "High-importance email analyzed" in json.dumps(demo.model.requests[1])
                preview = await demo.command(command.model_copy(update={"prepare": True}))
                original = demo.runtime.settings.platform.security
                demo.runtime.settings.platform.security = original.model_copy(
                    update={"user_grants": (READ, EXTRA)}
                )
                denied = await demo.api.post(
                    "/api/v1/platform/commands",
                    json=command.model_copy(
                        update={"confirmation_id": preview["confirmation_id"]}
                    ).model_dump(mode="json"),
                )
                assert denied.status_code == 403 and demo.model.receipts == []
                demo.runtime.settings.platform.security = original
                rechecked = True
            await demo.command(command, confirm=True)
    assert current.state == ExecutionState.SUCCEEDED, current
    assert current.output == {"demo_only": True, "sent": False}
    assert rechecked and {"platform_minimum", "workflow_approval"} <= reasons
    assert len(demo.model.receipts) == 1
    await demo.runtime.recovery.recover()
    await demo.runtime.wakeups.run_due()
    assert not await demo.runtime.execution_engine.run_once()
    assert len(demo.model.receipts) == 1


async def test_demo_3_user_skill_manual_workflow_and_proactive_permissions(demo: Demo) -> None:
    child = await demo.publish("Analyze Customer Incident", skill("Analyze Customer Incident V1"))
    parent = await demo.publish(
        "Incident workflow",
        workflow(
            call("incident", child, "input", "text"),
            dependencies=(child,),
            output={"text": binding("nodes", "incident", "text")},
        ),
    )
    tools = {tool.__name__: tool for tool in agent_tools(demo.runtime.platform)}
    direct = await tools["invoke_capability"](
        child.capability_id, 1, "incident-direct", '{"text":"Customer incident"}'
    )
    manual = await tools["invoke_capability"](
        parent.capability_id, 1, "incident-workflow", '{"text":"Customer incident"}'
    )
    service = await demo.service(configuration(parent))
    indirect = await demo.command(
        PlatformCommand(action="proactive.run", identifier=service["id"], request_key="incident-indirect")
    )
    for identifier in (direct["id"], manual["id"], indirect["id"]):
        result = await demo.pump(identifier)
        assert result.state == ExecutionState.SUCCEEDED
        assert result.output == {"text": "Incident analysis V1"}
    rows = (await demo.api.get("/api/v1/platform/executions")).json()["items"]
    children = [await demo.execution(row["id"]) for row in rows if row["parent_execution_id"]]
    assert len(children) == 2
    for execution in children:
        parent_execution = await demo.execution(execution.parent_execution_id)
        assert execution.snapshot.capability.number == 1
        assert execution.snapshot.granted_permissions == (READ,)
        assert set(execution.snapshot.granted_permissions) <= set(
            parent_execution.snapshot.granted_permissions
        )
        assert execution.correlation_id == parent_execution.correlation_id
    assert (await demo.execution(indirect["id"])).snapshot.granted_permissions == (READ,)


async def test_demo_4_running_version_and_dependency_lock_survive_v2(demo: Demo) -> None:
    child = await demo.publish("Analyze Customer Incident", skill("Analyze Customer Incident V1"))
    content = workflow(
        call("incident", child, "input", "text"),
        dependencies=(child,),
        output={"text": binding("nodes", "incident", "text")},
    )
    version = await demo.publish("Frozen incident workflow", content)
    exact = await demo.service(configuration(version))
    latest = await demo.service(configuration(version, latest=True))
    queued = await demo.command(
        PlatformCommand(
            action="execution.run",
            identifier=version.capability_id,
            version=1,
            request_key="running-v1",
            input={"text": "Incident"},
        )
    )
    demo.model.blocked_instruction = "Analyze Customer Incident V1"
    running = asyncio.create_task(
        demo.runtime.execution_engine.run(demo.runtime.platform.scope, queued["id"])
    )
    try:
        async with asyncio.timeout(10):
            await demo.model.entered.wait()
        original = (await demo.execution(queued["id"])).snapshot
        assert (await demo.execution(queued["id"])).state == ExecutionState.RUNNING
        replacements = []
        for previous, replacement in (
            (child, skill("Analyze Customer Incident V2")),
            (version, None),
        ):
            if replacement is None:
                replacement = workflow(
                    call("incident", replacements[0], "input", "text"),
                    dependencies=(replacements[0],),
                    output={"text": binding("nodes", "incident", "text")},
                )
            fork = await demo.command(
                PlatformCommand(action="capability.fork", identifier=previous.capability_id, version=1)
            )
            updated = await demo.command(
                PlatformCommand(
                    action="capability.update",
                    identifier=previous.capability_id,
                    version=2,
                    expected_revision=fork["revision"],
                    content=replacement,
                )
            )
            replacements.append(
                CapabilityVersion.model_validate(
                    await demo.command(
                        PlatformCommand(
                            action="capability.publish",
                            identifier=previous.capability_id,
                            version=2,
                            expected_revision=updated["revision"],
                        ),
                        confirm=True,
                    )
                )
            )
        new = []
        for service in (exact, latest):
            new.append(
                await demo.command(
                    PlatformCommand(
                        action="proactive.run",
                        identifier=service["id"],
                        request_key=f"after-v2-{service['id']}",
                    )
                )
            )
        demo.model.release.set()
        completed = await running
        assert completed.state == ExecutionState.SUCCEEDED
        assert completed.output == {"text": "Incident analysis V1"}
        assert completed.snapshot == original
        assert completed.snapshot.capability.content.dependency_lock == content.dependency_lock
        for queued_result, number in zip(new, (1, 2), strict=True):
            result = await demo.pump(queued_result["id"])
            assert result.state == ExecutionState.SUCCEEDED
            assert result.snapshot.capability.number == number
            assert result.snapshot.capability.content.dependency_lock.dependencies[0].version == number
            assert result.output == {"text": f"Incident analysis V{number}"}
    finally:
        demo.model.release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_demo_5_user_control_agrees_across_api_cli_and_agent(
    demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = await demo.publish("Controlled Skill", skill("Analyze Customer Incident V1"))
    service = await demo.service(configuration(child, trigger=EventTrigger(event_type="incident.received")))
    identifier = service["id"]
    tools = {tool.__name__: tool for tool in agent_tools(demo.runtime.platform)}
    paused = await tools["proactive_services"]("pause", identifier, service["revision"])
    loop = asyncio.get_running_loop()
    cli_results: list[dict[str, Any]] = []

    async def transport(_settings: Any, command: PlatformCommand, *, confirm: Any) -> dict[str, Any]:
        async def send(body: PlatformCommand) -> dict[str, Any]:
            response = await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(
                    demo.api.post("/api/v1/platform/commands", json=body.model_dump(mode="json")), loop
                )
            )
            return _decode(response)

        result = await _perform(send, command, confirm)
        cli_results.append(result)
        return result

    monkeypatch.setattr("shovel.cli.commands.platform.platform_command", transport)
    monkeypatch.setattr("shovel.cli.commands.platform.load_settings", lambda _: demo.runtime.settings)

    async def invoke(*arguments: str) -> dict[str, Any]:
        result = await asyncio.to_thread(CliRunner().invoke, cli, list(arguments), input="y\n")
        assert result.exit_code == 0, result.output
        return cli_results[-1]

    assert (await invoke("proactive", "show", identifier)) == paused
    assert (await demo.api.get(f"/api/v1/platform/proactive/{identifier}")).json() == paused
    config = ProactiveConfiguration.model_validate(paused["configuration"]).model_copy(
        update={
            "approval_preference": ApprovalPreference.RISK_BASED,
            "quiet_hours": QuietHours(timezone="UTC", start_minute=0, end_minute=1439),
        }
    )
    edited = await demo.command(
        PlatformCommand(
            action="proactive.update",
            identifier=identifier,
            expected_revision=paused["revision"],
            name="Controlled service",
            configuration=config,
        )
    )
    assert not edited["enabled"]
    descriptor = await tools["proactive_services"]("resume", identifier, edited["revision"])
    assert descriptor["requires_confirmation"]
    assert not (await demo.runtime.proactive.get(demo.runtime.platform.scope, identifier)).enabled
    resumed = await invoke("proactive", "resume", identifier)
    assert resumed["enabled"] and not resumed["paused"]
    assert (await tools["proactive_services"]("show", identifier)) == resumed
    assert (await demo.api.get(f"/api/v1/platform/proactive/{identifier}")).json() == resumed
    queued = await demo.command(
        PlatformCommand(action="proactive.run", identifier=identifier, request_key="control-manual")
    )
    assert (await demo.pump(queued["id"])).state == ExecutionState.SUCCEEDED
    explain = await tools["proactive_services"]("explain", identifier)
    assert explain["last_execution"]["id"] == queued["id"]
    assert explain["current_admission_reason"] == "quiet_hours"
    assert (await invoke("proactive", "explain", identifier)) == explain
    global_control = (await demo.api.get("/api/v1/platform/automation")).json()
    globally_paused = await demo.command(
        PlatformCommand(action="automation.pause", expected_revision=global_control["revision"])
    )
    assert (await tools["proactive_services"]("explain", identifier))[
        "current_admission_reason"
    ] == "globally_paused"
    assert (await invoke("proactive", "explain", identifier))["current_admission_reason"] == "globally_paused"
    assert (await demo.api.get("/api/v1/platform/automation")).json() == globally_paused
    assert (await tools["proactive_services"]("show", identifier)) == resumed
    await demo.command(
        PlatformCommand(action="automation.resume", expected_revision=globally_paused["revision"]),
        confirm=True,
    )
    assert not (await demo.api.get("/api/v1/platform/automation")).json()["paused"]
    assert (await invoke("proactive", "show", identifier)) == resumed
