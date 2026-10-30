from __future__ import annotations

import ast
import asyncio
from itertools import pairwise
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from typer.testing import CliRunner

from shovel.capability.domain import (
    CapabilityContent,
    CapabilityKind,
    Permission,
    SkillImplementation,
    WorkflowEdge,
    WorkflowImplementation,
    WorkflowNode,
)
from shovel.cli.app import app
from shovel.platform.agent_tools import agent_tools
from shovel.platform.domain import PlatformCommand
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.hosting import build_cli_host, resolve_runtime
from shovel.runtime.platform_client import _perform
from shovel.runtime.service import Runtime
from shovel.security.domain import SecurityDenied, UserSecurityPolicy
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime
from test_hosted_runtime import settings_at


def graph(*middle: WorkflowNode) -> CapabilityContent:
    nodes = (WorkflowNode(id="start", kind="input"), *middle, WorkflowNode(id="end", kind="output"))
    return CapabilityContent(
        implementation=WorkflowImplementation(
            nodes=nodes,
            edges=tuple(WorkflowEdge(source=left.id, target=right.id) for left, right in pairwise(nodes)),
        )
    )


async def create(runtime: Runtime, content: CapabilityContent | None = None) -> dict:
    content = content or CapabilityContent(implementation=SkillImplementation(instructions="No model calls."))
    return await runtime.platform.execute(
        PlatformCommand(
            action="capability.create",
            kind=CapabilityKind(content.implementation.kind),
            name="Entry test",
            content=content,
        ),
        human=True,
    )


async def confirmed(runtime: Runtime, command: PlatformCommand) -> dict:
    preview = await runtime.platform.execute(command.model_copy(update={"prepare": True}), human=True)
    return await runtime.platform.execute(
        command.model_copy(
            update={"confirmation_id": preview["confirmation_id"]},
        ),
        human=True,
    )


async def publish(runtime: Runtime, content: CapabilityContent) -> dict:
    draft = await create(runtime, content)
    return await confirmed(
        runtime,
        PlatformCommand(
            action="capability.publish",
            identifier=draft["capability_id"],
            version=1,
            expected_revision=1,
        ),
    )


async def run(runtime: Runtime, content: CapabilityContent, key: str = "entry-test") -> dict:
    version = await publish(runtime, content)
    queued = await runtime.platform.execute(
        PlatformCommand(
            action="execution.run",
            identifier=version["capability_id"],
            version=1,
            request_key=key,
        ),
        human=True,
    )
    await runtime.execution_engine.run(runtime.platform.scope, queued["id"])
    return await runtime.platform.execute(
        PlatformCommand(action="execution.show", identifier=queued["id"]), human=True
    )


async def test_api_drafts_explicit_publish_and_replay(runtime: Runtime, api: httpx.AsyncClient) -> None:
    body = PlatformCommand(
        action="capability.create", kind=CapabilityKind.WORKFLOW, name="API workflow", content=graph()
    ).model_dump(mode="json")
    response = await api.post("/api/v1/platform/commands", json=body)
    assert response.status_code == 200
    draft = response.json()
    command = PlatformCommand(
        action="capability.publish", identifier=draft["capability_id"], version=1, expected_revision=1
    )
    assert (
        await api.post("/api/v1/platform/commands", json=command.model_dump(mode="json"))
    ).status_code == 409
    preview = (
        await api.post(
            "/api/v1/platform/commands",
            json=command.model_copy(
                update={"prepare": True},
            ).model_dump(mode="json"),
        )
    ).json()
    assert preview["preview"]["content"]["implementation"]["kind"] == "workflow"
    payload = command.model_copy(update={"confirmation_id": preview["confirmation_id"]}).model_dump(
        mode="json"
    )
    published = await api.post("/api/v1/platform/commands", json=payload)
    assert published.status_code == 200 and published.json()["published_at"] is not None
    assert (await api.post("/api/v1/platform/commands", json=payload)).status_code == 409
    assert (await api.get("/api/v1/platform/capabilities")).json()["items"][0]["id"] == draft["capability_id"]
    assert (await api.post("/api/v1/platform/commands", json={**body, "human": True})).status_code == 422
    assert (
        await api.post(
            "/api/v1/platform/commands", json=body, headers={"Origin": "https://untrusted.invalid"}
        )
    ).status_code == 403


async def test_changed_draft_invalidates_publish_confirmation(runtime: Runtime) -> None:
    draft = await create(runtime)
    command = PlatformCommand(
        action="capability.publish", identifier=draft["capability_id"], version=1, expected_revision=1
    )
    preview = await runtime.platform.execute(command.model_copy(update={"prepare": True}), human=True)
    await runtime.platform.capabilities.update_draft(
        runtime.platform.scope,
        draft["capability_id"],
        1,
        CapabilityContent(implementation=SkillImplementation(instructions="Changed.")),
        expected_revision=1,
    )
    with pytest.raises(RuntimeFailure, match="changed"):
        await runtime.platform.execute(
            command.model_copy(update={"confirmation_id": preview["confirmation_id"]}), human=True
        )


async def test_agent_draft_and_api_visible_state_agree(runtime: Runtime, api: httpx.AsyncClient) -> None:
    tools = {tool.__name__: tool for tool in agent_tools(runtime.platform)}
    draft = await tools["create_skill_draft"](
        "Agent draft", '{"implementation":{"kind":"skill","instructions":"Read only."}}'
    )
    assert draft["published_at"] is None
    versions = await tools["discover_capabilities"]("versions", draft["capability_id"])
    assert (
        versions["items"]
        == (
            await runtime.platform.execute(
                PlatformCommand(
                    action="capability.versions",
                    identifier=draft["capability_id"],
                ),
                human=True,
            )
        )["items"]
    )
    assert (
        await api.get(f"/api/v1/platform/capabilities/{draft['capability_id']}?version=1")
    ).json() == draft
    validation = await tools["validate_draft"](draft["capability_id"], 1)
    assert validation["valid"]


async def test_agent_cannot_consume_user_publish_ticket(runtime: Runtime) -> None:
    draft = await create(runtime)
    command = PlatformCommand(
        action="capability.publish", identifier=draft["capability_id"], version=1, expected_revision=1
    )
    preview = await runtime.platform.execute(command.model_copy(update={"prepare": True}), human=True)
    result = await runtime.platform.execute(
        command.model_copy(update={"confirmation_id": preview["confirmation_id"]}), human=False
    )
    assert result["requires_confirmation"] and "confirmation_id" not in result["command"]
    assert (
        await runtime.capabilities.get_version(runtime.platform.scope, draft["capability_id"], 1)
    ).published_at is None


async def test_approval_structured_ids_and_agent_good_is_not_consent(
    runtime: Runtime, api: httpx.AsyncClient
) -> None:
    execution = await run(runtime, graph(WorkflowNode(id="approve", kind="approval")))
    approvals = (await api.get("/api/v1/platform/approvals")).json()["items"]
    approval = approvals[0]
    tools = {tool.__name__: tool for tool in agent_tools(runtime.platform)}
    result = await tools["execution_human_action"]("approve", execution["id"], approval["id"])
    assert result["requires_confirmation"]
    unchanged = await runtime.approvals.get(
        runtime.platform.scope, execution["id"], approval["id"], user_id=runtime.platform.scope.user_id
    )
    assert unchanged.state == "pending"
    command = PlatformCommand(
        action="approval.approve", identifier=approval["id"], execution_id=execution["id"]
    )
    assert (
        await api.post("/api/v1/platform/commands", json=command.model_dump(mode="json"))
    ).status_code == 409
    wrong = command.model_copy(update={"execution_id": "wrong"})
    assert (
        await api.post(
            "/api/v1/platform/commands",
            json=wrong.model_copy(update={"prepare": True}).model_dump(mode="json"),
        )
    ).status_code == 404
    await confirmed(runtime, command)
    await runtime.wakeups.run_due()
    await runtime.execution_engine.run(runtime.platform.scope, execution["id"])
    shown = await tools["view_execution"](execution["id"])
    assert shown == (await api.get(f"/api/v1/platform/executions/{execution['id']}")).json()
    assert shown["state"] == "succeeded"


async def test_explicit_input_response_is_bound_and_resumes(runtime: Runtime) -> None:
    execution = await run(
        runtime,
        graph(
            WorkflowNode(
                id="ask",
                kind="ask_user",
                prompt="Input",
                response_schema={"type": "string"},
            )
        ),
    )
    interactions = await runtime.platform.execute(PlatformCommand(action="interaction.list"), human=True)
    request = next(row for row in interactions["items"] if row["kind"] == "input_request")
    command = PlatformCommand(
        action="interaction.input",
        identifier=request["source_id"],
        execution_id=execution["id"],
        response="好的",
    )
    descriptor = await runtime.platform.execute(command, human=False)
    assert descriptor["requires_confirmation"]
    await confirmed(runtime, command)
    await runtime.wakeups.run_due()
    result = await runtime.execution_engine.run(runtime.platform.scope, execution["id"])
    assert result.state == "succeeded"


async def test_agent_permission_ceiling_and_frozen_user_grants(runtime: Runtime) -> None:
    read = Permission(action="read", resource="knowledge")
    runtime.settings.platform.security = UserSecurityPolicy(user_grants=(read,), platform_constraints=(read,))
    version = await publish(
        runtime,
        CapabilityContent(
            implementation=SkillImplementation(instructions="No model."),
            required_permissions=(read,),
            permission_boundary=(read,),
        ),
    )
    command = PlatformCommand(
        action="execution.run", identifier=version["capability_id"], version=1, request_key="scoped"
    )
    with pytest.raises(SecurityDenied):
        await runtime.platform.execute(command, human=False)
    with pytest.raises(SecurityDenied, match="narrow"):
        await runtime.platform.execute(command.model_copy(update={"permissions": (read,)}), human=False)
    runtime.settings.platform.agent_permissions = (read,)
    execution = await runtime.platform.execute(command, human=False)
    assert execution["snapshot"]["granted_permissions"] == [read.model_dump()]
    runtime.settings.platform.security = UserSecurityPolicy()
    assert await runtime.execution_engine.run_once()
    assert (
        await runtime.execution_engine.repository.get(runtime.platform.scope, execution["id"])
    ).state == "failed"


async def test_proactive_draft_enable_confirmation_and_explain_history(runtime: Runtime) -> None:
    from shovel.automation.domain import ManualTrigger, ProactiveConfiguration, TargetCapability

    version = await publish(runtime, graph())
    draft = await runtime.platform.execute(
        PlatformCommand(
            action="proactive.create",
            configuration=ProactiveConfiguration(
                trigger=ManualTrigger(),
                target=TargetCapability(capability_id=version["capability_id"], version=1),
            ),
        ),
        human=False,
    )
    assert not draft["enabled"]
    command = PlatformCommand(action="proactive.enable", identifier=draft["id"], expected_revision=1)
    assert (await runtime.platform.execute(command, human=False))["requires_confirmation"]
    await confirmed(runtime, command)
    execution = await runtime.platform.execute(
        PlatformCommand(action="proactive.run", identifier=draft["id"], request_key="manual-service"),
        human=True,
    )
    explained = await runtime.platform.execute(
        PlatformCommand(action="proactive.explain", identifier=draft["id"]), human=False
    )
    assert explained["last_execution"]["id"] == execution["id"]
    assert explained["last_execution"]["snapshot"]["context"]["proactive_snapshot"]["revision"] == 2
    paused = await runtime.platform.execute(
        PlatformCommand(action="proactive.pause", identifier=draft["id"], expected_revision=2), human=False
    )
    assert paused["enabled"] and paused["paused"]


async def test_cli_transport_uses_same_confirmation_service(runtime: Runtime) -> None:
    draft = await create(runtime)
    command = PlatformCommand(action="capability.publish", identifier=draft["capability_id"], version=1)

    async def send(body: PlatformCommand) -> dict:
        return await runtime.platform.execute(body, human=True)

    seen = []

    def accept(preview: dict) -> bool:
        seen.append(preview)
        return True

    result = await _perform(send, command, accept)
    assert result["published_at"] and seen[0]["preview"]["capability_id"] == draft["capability_id"]


async def test_confirmation_expiry_and_direct_publish_are_denied(runtime: Runtime) -> None:
    draft = await create(runtime)
    command = PlatformCommand(
        action="capability.publish", identifier=draft["capability_id"], version=1, expected_revision=1
    )
    with pytest.raises(SecurityDenied, match="explicit"):
        await runtime.platform.capabilities.publish(
            runtime.platform.scope, draft["capability_id"], 1, expected_revision=1
        )
    preview = await runtime.platform.execute(command.model_copy(update={"prepare": True}), human=True)
    runtime.platform.clock = lambda: preview["expires_at"] + 1
    with pytest.raises(RuntimeFailure, match="expired"):
        await runtime.platform.execute(
            command.model_copy(update={"confirmation_id": preview["confirmation_id"]}), human=True
        )


async def test_agent_can_create_workflow_and_disabled_service_drafts(runtime: Runtime) -> None:
    from shovel.automation.domain import ManualTrigger, ProactiveConfiguration, TargetCapability

    tools = {tool.__name__: tool for tool in agent_tools(runtime.platform)}
    workflow = await tools["create_workflow_draft"]("Draft", graph().model_dump_json())
    assert workflow["published_at"] is None
    service = await tools["create_proactive_draft"](
        ProactiveConfiguration(
            trigger=ManualTrigger(),
            target=TargetCapability(capability_id=workflow["capability_id"], version=1),
        ).model_dump_json()
    )
    assert not service["enabled"]
    validation = await tools["validate_draft"](service["id"], proactive=True)
    assert not validation["valid"] and validation["errors"]


async def test_api_invocation_idempotency_scope_and_permission_enforcement(
    runtime: Runtime, api: httpx.AsyncClient
) -> None:
    version = await publish(runtime, graph())
    command = PlatformCommand(action="execution.run", identifier=version["capability_id"], version=1)
    payload = command.model_dump(mode="json")
    first = await api.post("/api/v1/platform/commands", json=payload, headers={"Idempotency-Key": "api-run"})
    assert first.status_code == 202
    second = await api.post("/api/v1/platform/commands", json=payload, headers={"Idempotency-Key": "api-run"})
    assert second.json()["id"] == first.json()["id"]
    changed = {**payload, "input": {"changed": True}}
    assert (
        await api.post("/api/v1/platform/commands", json=changed, headers={"Idempotency-Key": "api-run"})
    ).status_code == 409
    from shovel.capability.service import CapabilityService

    foreign = runtime.platform.scope.model_copy(update={"user_id": "other"})
    draft = await CapabilityService(runtime.capabilities).create_draft(
        foreign,
        CapabilityKind.SKILL,
        "Private",
        CapabilityContent(implementation=SkillImplementation(instructions="Private.")),
    )
    assert (await api.get(f"/api/v1/platform/capabilities/{draft.capability_id}")).status_code == 404
    read = Permission(action="read", resource="knowledge")
    protected = await publish(
        runtime,
        graph().model_copy(
            update={
                "required_permissions": (read,),
                "permission_boundary": (read,),
            }
        ),
    )
    denied = PlatformCommand(
        action="execution.run", identifier=protected["capability_id"], version=1, request_key="denied"
    )
    assert (
        await api.post("/api/v1/platform/commands", json=denied.model_dump(mode="json"))
    ).status_code == 403


async def test_reject_remains_possible_after_permission_revocation(runtime: Runtime) -> None:
    read = Permission(action="read", resource="knowledge")
    runtime.settings.platform.security = UserSecurityPolicy(user_grants=(read,), platform_constraints=(read,))
    execution = await run(
        runtime,
        graph(WorkflowNode(id="approve", kind="approval")).model_copy(
            update={
                "required_permissions": (read,),
                "permission_boundary": (read,),
            }
        ),
    )
    approval = (await runtime.platform.execute(PlatformCommand(action="approval.list"), human=True))["items"][
        0
    ]
    runtime.settings.platform.security = UserSecurityPolicy()
    command = PlatformCommand(
        action="approval.approve", identifier=approval["id"], execution_id=execution["id"]
    )
    with pytest.raises(SecurityDenied):
        await confirmed(runtime, command)
    rejected = await confirmed(runtime, command.model_copy(update={"action": "approval.reject"}))
    assert rejected["state"] == "rejected"


async def test_internal_failure_logs_safe_stack_not_exception_body(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        runtime.platform.queries, "capabilities", AsyncMock(side_effect=RuntimeError("private-error-marker"))
    )
    with pytest.raises(RuntimeError, match="private-error-marker"):
        await runtime.platform.execute(PlatformCommand(action="capability.list"), human=True)
    output = capsys.readouterr()
    assert "private-error-marker" not in output.out + output.err


def test_real_cli_explicit_publish_and_offline_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_at(tmp_path)

    async def seed() -> tuple[dict, dict, dict]:
        host, provider = build_cli_host(settings)
        try:
            await host.start_async()
            runtime = await resolve_runtime(provider)
            workflow = await create(runtime, graph())
            skill = await create(runtime)
            from shovel.automation.domain import ManualTrigger, ProactiveConfiguration, TargetCapability

            service = await runtime.platform.execute(
                PlatformCommand(
                    action="proactive.create",
                    configuration=ProactiveConfiguration(
                        trigger=ManualTrigger(),
                        target=TargetCapability(capability_id=workflow["capability_id"], version=1),
                    ),
                ),
                human=True,
            )
            return workflow, skill, service
        finally:
            host.lifetime.stop_application()
            await host.stop_async()

    draft, skill, service = asyncio.run(seed())
    monkeypatch.setattr("shovel.cli.commands.platform.load_settings", lambda _: settings)
    runner = CliRunner()
    denied = runner.invoke(app, ["workflow", "publish", draft["capability_id"], "1"], input="n\n")
    assert denied.exit_code != 0
    published = runner.invoke(app, ["workflow", "publish", draft["capability_id"], "1"], input="y\n")
    assert published.exit_code == 0, published.output
    result = runner.invoke(
        app, ["--request-key", "cli-workflow", "workflow", "run", draft["capability_id"], "1"]
    )
    assert result.exit_code == 0 and '"state": "succeeded"' in result.output, result.output
    assert runner.invoke(app, ["capability", "list"]).exit_code == 0
    assert runner.invoke(app, ["workflow", "versions", draft["capability_id"]]).exit_code == 0
    for args in (
        ["skill", "list"],
        ["skill", "show", skill["capability_id"]],
        ["skill", "validate", skill["capability_id"], "1"],
        ["skill", "versions", skill["capability_id"]],
        ["capability", "search", "Entry"],
        ["execution", "list"],
        ["proactive", "show", service["id"]],
        ["proactive", "explain", service["id"]],
        ["proactive", "enable", service["id"]],
        ["proactive", "pause", service["id"]],
        ["proactive", "resume", service["id"]],
        ["proactive", "run", service["id"]],
        ["proactive", "history", service["id"]],
        ["proactive", "disable", service["id"]],
        ["approval", "list"],
    ):
        response = runner.invoke(app, args, input="y\n")
        assert response.exit_code == 0, response.output
    invalid = runner.invoke(app, ["skill", "show", "bad id"])
    assert invalid.exit_code != 0 and "Invalid command parameters" in invalid.output


@pytest.mark.parametrize(
    "relative",
    [
        "platform",
        "runtime\\platform_api.py",
        "runtime\\platform_client.py",
        "cli\\commands\\platform.py",
    ],
)
def test_new_entry_points_never_access_orm(relative: str) -> None:
    target = Path(__file__).parent.parent / "src" / "shovel" / relative
    for path in target.glob("*.py") if target.is_dir() else [target]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("sqlalchemy", "shovel.db")), path
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"sessions", "execute_sql"}, path
