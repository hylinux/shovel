from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from scripts.memory_baseline import baseline_runtime
from sqlalchemy import delete, func, select, text
from typer.testing import CliRunner

from shovel.capability.domain import (
    CapabilityContent,
    CapabilityKind,
    CapabilityScope,
    Condition,
    InputBinding,
    SkillImplementation,
    WorkflowEdge,
    WorkflowImplementation,
    WorkflowNode,
)
from shovel.cli.app import app
from shovel.db.migrations import upgrade_database
from shovel.db.model.memory import MemoryExperience, MemoryIndexOutbox
from shovel.db.model.retrieval import SchemaMigration
from shovel.db.model.runtime import Operation
from shovel.execution.domain import Execution, ExecutionState
from shovel.memory.experience import ExperienceSubmission, evidence
from shovel.platform.domain import PlatformCommand
from shovel.protection.domain import ProtectionDeniedError
from shovel.protection.egress import egress_scope, mark_enterprise
from shovel.runtime.diagnostics import development_logging
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.service import Runtime
from shovel.security.domain import SecurityDenied
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime
from test_hosted_runtime import settings_at
from test_platform_entries import publish, run


def graph(value: object = "sorted") -> CapabilityContent:
    return CapabilityContent(
        implementation=WorkflowImplementation(
            nodes=(
                WorkflowNode(id="start", kind="input"),
                WorkflowNode(
                    id="end",
                    kind="output",
                    input_mapping={"result": InputBinding(source="literal", value=value)},
                ),
            ),
            edges=(WorkflowEdge(source="start", target="end"),),
        )
    )


def submission(expected: object = "sorted") -> dict:
    return {
        "problem": "Check a deterministic local workflow result.",
        "environment": ["Isolated local store; no external effects."],
        "applicability": ["Only this frozen workflow and the checked output fields."],
        "checks": [{"path": ["result"], "expected": expected}],
    }


async def collect(runtime: Runtime, source: dict, body: dict | None = None) -> dict:
    return await runtime.platform.execute(
        PlatformCommand(
            action="experience.collect",
            execution_id=source["id"],
            expected_revision=source["revision"],
            input=body or submission(),
        ),
        human=True,
    )


async def test_api_receipt_review_and_forget_never_activate_or_resurrect(
    runtime: Runtime,
    api: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = await run(runtime, graph())
    extractor = AsyncMock(side_effect=AssertionError("Experience must not call an extractor."))
    monkeypatch.setattr(runtime.memory.extractor, "extract", extractor)
    command = PlatformCommand(
        action="experience.collect",
        execution_id=source["id"],
        expected_revision=source["revision"],
        input=submission(),
    )
    with development_logging():
        response = await api.post("/api/v1/platform/commands", json=command.model_dump(mode="json"))
        assert response.status_code == 200, response.text
        candidate = response.json()
        identifier = candidate["id"]
        assert candidate["state"] == "candidate" and not candidate["recall_eligible"]
        assert candidate["evidence"]["observed_outcome"] == "passed"
        assert candidate["evidence"]["annotations_source"] == "human_unverified"
        assert candidate["evidence"]["source"]["execution_revision"] == source["revision"]
        assert (await api.get("/api/v1/platform/experiences")).json()["items"] == [candidate]
        assert (await api.get(f"/api/v1/platform/experiences/{identifier}")).json() == candidate
        assert await collect(runtime, source) == candidate
        review = PlatformCommand(
            action="experience.review",
            identifier=identifier,
            expected_revision=1,
            input={"decision": "retain", "note": "Checked the recorded output, not business success."},
        )
        kept = await api.post("/api/v1/platform/commands", json=review.model_dump(mode="json"))
        assert kept.status_code == 200, kept.text
        assert kept.json()["state"] == "reviewed" and not kept.json()["recall_eligible"]
        assert (
            await api.post("/api/v1/platform/commands", json=review.model_dump(mode="json"))
        ).status_code == 409
        assert await runtime.memory.list(include_history=True) == []
        assert await runtime.memory.search("deterministic workflow result") == []
        assert (await api.get(f"/api/v1/memories/{identifier}")).status_code == 404
        async with runtime.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(MemoryIndexOutbox)) == 0
        forget = PlatformCommand(
            action="experience.forget",
            identifier=identifier,
            expected_revision=2,
        )
        erased = await api.post("/api/v1/platform/commands", json=forget.model_dump(mode="json"))
        assert erased.status_code == 200 and erased.json()["state"] == "deleted"
        assert erased.json()["evidence"] == erased.json()["review"] == {}
        replay = await collect(runtime, source)
        assert replay == erased.json()
        with pytest.raises(RuntimeFailure, match="different experience"):
            await collect(runtime, source, submission("different"))
    extractor.assert_not_called()
    output = capsys.readouterr().out
    assert "memory.experience.collected" in output and "memory.experience.forgotten" in output
    assert "deterministic local" not in output and "business success" not in output
    assert str(runtime.settings.agent.workspace) not in output


@pytest.mark.parametrize(
    ("actual", "expected", "outcome"),
    [
        ("sorted", "sorted", "passed"),
        ("unsorted", "sorted", "failed"),
        ({"count": True}, {"count": 1}, "failed"),
        ({"a": 1, "b": 2}, {"b": 2, "a": 1}, "passed"),
    ],
)
async def test_server_checks_outputs_not_completed_or_python_coercion(
    runtime: Runtime,
    actual: object,
    expected: object,
    outcome: str,
) -> None:
    source = await run(runtime, graph(actual))
    candidate = await collect(runtime, source, submission(expected))
    assert source["state"] == "succeeded"
    assert candidate["evidence"]["observed_outcome"] == outcome
    assert candidate["evidence"]["observations"][0]["passed"] == (outcome == "passed")
    assert candidate["state"] == "candidate" and not candidate["recall_eligible"]


async def test_missing_output_is_not_verified_null(runtime: Runtime) -> None:
    source = await run(runtime, graph(None))
    body = submission(None)
    body["checks"].append({"path": ["missing"], "expected": None})
    candidate = await collect(runtime, source, body)
    observations = candidate["evidence"]["observations"]
    assert observations[0]["exists"] and observations[0]["passed"]
    assert not observations[1]["exists"] and not observations[1]["passed"]
    assert candidate["evidence"]["failure_reason"] == "output_assertion_mismatch"


async def test_condition_workflow_observation_is_separate_from_execution_success(runtime: Runtime) -> None:
    content = CapabilityContent(
        input_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
        implementation=WorkflowImplementation(
            nodes=(
                WorkflowNode(id="start", kind="input"),
                WorkflowNode(
                    id="check",
                    kind="condition",
                    condition=Condition(path=("input", "count"), operator="gt", value=0),
                ),
                WorkflowNode(
                    id="end",
                    kind="output",
                    input_mapping={
                        "result": InputBinding(source="context", path=("nodes", "check", "result"))
                    },
                ),
            ),
            edges=(
                WorkflowEdge(source="start", target="check"),
                WorkflowEdge(source="check", target="end", label="true"),
                WorkflowEdge(source="check", target="end", label="false"),
            ),
        ),
    )
    version = await publish(runtime, content)
    queued = await runtime.platform.execute(
        PlatformCommand(
            action="execution.run",
            identifier=version["capability_id"],
            version=1,
            input={"count": 0},
            request_key="condition-experience",
        ),
        human=True,
    )
    execution = await runtime.execution_engine.run(runtime.platform.scope, queued["id"])
    candidate = await collect(runtime, execution.model_dump(mode="json"), submission(True))
    assert execution.state == ExecutionState.SUCCEEDED and execution.output == {"result": False}
    assert candidate["evidence"]["observed_outcome"] == "failed"
    assert candidate["evidence"]["attempt"]["node_kinds"] == ["input", "condition", "output"]
    assert candidate["evidence"]["attempt"]["completed_node_ids"] == ["start", "check"]


async def test_api_cannot_supply_trusted_verification_or_activation_fields(
    runtime: Runtime,
    api: httpx.AsyncClient,
) -> None:
    source = await run(runtime, graph())
    for fields in ({"verified": True}, {"state": "active"}, {"checks": []}):
        command = PlatformCommand(
            action="experience.collect",
            execution_id=source["id"],
            expected_revision=source["revision"],
            input={**submission(), **fields},
        )
        response = await api.post("/api/v1/platform/commands", json=command.model_dump(mode="json"))
        assert response.status_code == 422, response.text
    keyed = PlatformCommand(
        action="experience.collect",
        execution_id=source["id"],
        expected_revision=source["revision"],
        input=submission(),
    )
    response = await api.post(
        "/api/v1/platform/commands",
        json=keyed.model_dump(mode="json"),
        headers={"Idempotency-Key": "not-a-source-identity"},
    )
    assert response.status_code == 422 and "omit request_key" in response.json()["error"]["message"]
    assert (await api.get("/api/v1/platform/experiences")).json()["items"] == []


@pytest.mark.parametrize("change", ["model", "external_node", "nonmanual", "unfinished", "enterprise"])
async def test_ineligible_source_never_becomes_experience(runtime: Runtime, change: str) -> None:
    source = await run(runtime, graph())
    execution = await runtime.invocations.repository.get(runtime.platform.scope, source["id"])
    data = execution.model_dump(mode="json")
    if change == "model":
        content = CapabilityContent(implementation=SkillImplementation(instructions="Claim success."))
        data["snapshot"]["capability"].update(
            kind=CapabilityKind.SKILL,
            content=content.model_dump(mode="json"),
            content_hash=content.content_hash,
        )
    elif change == "external_node":
        content = graph()
        nodes = (
            *content.implementation.nodes[:1],
            WorkflowNode(id="notify", kind="notification"),
            content.implementation.nodes[-1],
        )
        content = CapabilityContent(
            implementation=WorkflowImplementation(
                nodes=nodes,
                edges=(
                    WorkflowEdge(source="start", target="notify"),
                    WorkflowEdge(source="notify", target="end"),
                ),
            )
        )
        data["snapshot"]["capability"].update(
            content=content.model_dump(mode="json"), content_hash=content.content_hash
        )
    elif change == "nonmanual":
        data["snapshot"]["context"]["source"] = "agent"
    elif change == "unfinished":
        data["state"] = ExecutionState.RUNNING
    else:
        data["snapshot"]["enterprise_rollout_binding"] = "a" * 64
    with pytest.raises(RuntimeFailure, match="pure Input"):
        evidence(Execution.model_validate(data), ExperienceSubmission.model_validate(submission()))


async def test_scope_and_human_boundary_are_not_supplied_by_request(runtime: Runtime) -> None:
    source = await run(runtime, graph())
    candidate = await collect(runtime, source)
    service = runtime.platform.experiences
    assert service is not None
    for scope in (
        CapabilityScope(workspace_id="other", user_id=runtime.platform.scope.user_id),
        CapabilityScope(workspace_id=runtime.platform.scope.workspace_id, user_id="other"),
    ):
        assert await service.repository.list(scope) == []
        with pytest.raises(NotFound):
            await service.repository.get(scope, candidate["id"])
        with pytest.raises(NotFound):
            await service.collect(
                scope, source["id"], source["revision"], ExperienceSubmission.model_validate(submission())
            )
    for action in (
        "experience.list",
        "experience.show",
        "experience.collect",
        "experience.review",
        "experience.forget",
    ):
        with pytest.raises(SecurityDenied, match="explicit human"):
            await runtime.platform.execute(PlatformCommand.model_validate({"action": action}), human=False)


async def test_epoch_and_source_checks_fence_commit_and_review(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = await run(runtime, graph())
    service = runtime.platform.experiences
    assert service is not None
    original = service.sources.get

    async def forgotten(scope: CapabilityScope, identifier: str) -> Execution:
        execution = await original(scope, identifier)
        await runtime.memory_tasks.invalidate()
        return execution

    with monkeypatch.context() as patch:
        patch.setattr(service.sources, "get", forgotten)
        with pytest.raises(RuntimeFailure, match="generation changed"):
            await collect(runtime, source)
    candidate = await collect(runtime, source)
    await runtime.memory_tasks.invalidate()
    invalid = await service.repository.get(runtime.platform.scope, candidate["id"])
    assert invalid["state"] == "invalidated" and invalid["invalid_reason"] == "memory_generation_changed"
    with pytest.raises(RuntimeFailure, match="no longer current"):
        await runtime.platform.execute(
            PlatformCommand(
                action="experience.review",
                identifier=candidate["id"],
                expected_revision=1,
                input={"decision": "retain", "note": "Cannot override invalidation."},
            ),
            human=True,
        )
    rejected = await runtime.platform.execute(
        PlatformCommand(
            action="experience.review",
            identifier=candidate["id"],
            expected_revision=1,
            input={"decision": "reject", "note": "Discard stale source."},
        ),
        human=True,
    )
    assert rejected["state"] == "rejected"
    assert (await collect(runtime, source))["state"] == "rejected"
    with pytest.raises(RuntimeFailure, match="no longer reviewable"):
        await runtime.platform.execute(
            PlatformCommand(
                action="experience.review",
                identifier=candidate["id"],
                expected_revision=2,
                input={"decision": "retain", "note": "Cannot restore rejection."},
            ),
            human=True,
        )


async def test_stale_source_revision_and_changed_output_are_rechecked(runtime: Runtime) -> None:
    source = await run(runtime, graph())
    with pytest.raises(RuntimeFailure, match="source Execution has changed"):
        await collect(runtime, {**source, "revision": source["revision"] - 1})
    service = runtime.platform.experiences
    assert service is not None
    execution = await service.sources.get(runtime.platform.scope, source["id"])
    body = ExperienceSubmission.model_validate(submission())
    stale = execution.model_copy(update={"output": {"result": "forged"}})
    with pytest.raises(RuntimeFailure, match="source changed before"):
        await service.repository.capture(
            runtime.platform.scope,
            stale,
            "a" * 64,
            evidence(stale, body),
            await service.repository.generation(runtime.platform.scope),
        )
    candidate = await collect(runtime, source)
    async with runtime.sessions() as session:
        row = await session.get(Operation, source["id"])
        assert row is not None
        row.row_version += 1
        await session.commit()
    invalid = await service.repository.get(runtime.platform.scope, candidate["id"])
    assert invalid["state"] == "invalidated" and invalid["invalid_reason"] == "source_changed"
    with pytest.raises(RuntimeFailure, match="no longer current"):
        await runtime.platform.execute(
            PlatformCommand(
                action="experience.review",
                identifier=candidate["id"],
                expected_revision=1,
                input={"decision": "retain", "note": "The captured source changed."},
            ),
            human=True,
        )


async def test_source_disappearance_invalidates_candidate_without_recreation(runtime: Runtime) -> None:
    source = await run(runtime, graph())
    candidate = await collect(runtime, source)
    async with runtime.sessions() as session:
        await session.execute(
            text("DELETE FROM execution_step WHERE execution_id = :id"), {"id": source["id"]}
        )
        await session.execute(
            text("DELETE FROM execution_attempt WHERE execution_id = :id"), {"id": source["id"]}
        )
        await session.execute(text("DELETE FROM operation WHERE id = :id"), {"id": source["id"]})
        await session.commit()
    service = runtime.platform.experiences
    assert service is not None
    result = await service.repository.get(runtime.platform.scope, candidate["id"])
    assert result["state"] == "invalidated" and result["invalid_reason"] == "source_unavailable"
    with pytest.raises(NotFound):
        await collect(runtime, source)


async def test_protected_flow_and_credentials_are_denied_before_persistence(runtime: Runtime) -> None:
    source = await run(runtime, graph())
    with egress_scope():
        mark_enterprise()
        with pytest.raises(ProtectionDeniedError):
            await collect(runtime, source)
        async with runtime.sessions() as session:
            session.add(
                MemoryExperience(
                    workspace_id="w",
                    user_id="u",
                    source_execution_id="blocked",
                    source_digest="a" * 64,
                    submission_digest="b" * 64,
                    generation=0,
                )
            )
            with pytest.raises(ProtectionDeniedError):
                await session.commit()
    with pytest.raises(ValidationError, match="Credentials"):
        ExperienceSubmission.model_validate({**submission(), "problem": "password=not-a-real-secret"})
    with pytest.raises(ValidationError):
        ExperienceSubmission.model_validate({**submission(), "checks": []})
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryExperience)) == 0


async def test_candidate_survives_full_runtime_restart_without_learning(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    async with baseline_runtime(settings) as first:
        source = await run(first, graph())
        candidate = await collect(first, source)
    async with baseline_runtime(settings) as second:
        result = await second.platform.execute(
            PlatformCommand(
                action="experience.show",
                identifier=candidate["id"],
            ),
            human=True,
        )
        assert result == candidate
        assert await second.memory.list(include_history=True) == []
        assert (await collect(second, source))["id"] == candidate["id"]


async def test_version_24_upgrade_creates_candidate_store_without_backfill(runtime: Runtime) -> None:
    engine = runtime.sessions.kw["bind"]
    async with engine.begin() as connection:
        await connection.exec_driver_sql("DROP TABLE memory_experience")
        await connection.execute(delete(SchemaMigration).where(SchemaMigration.version == 25))
    await upgrade_database(engine)
    await upgrade_database(engine)
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MemoryExperience)) == 0
        assert (
            await session.scalar(
                select(func.count()).select_from(SchemaMigration).where(SchemaMigration.version == 25)
            )
            == 1
        )


def test_experience_cli_uses_same_platform_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_at(tmp_path)

    async def seed() -> dict:
        async with baseline_runtime(settings) as current:
            return await run(current, graph())

    source = asyncio.run(seed())
    path = tmp_path / "experience.json"
    path.write_text(json.dumps(submission()), encoding="utf-8")
    monkeypatch.setattr("shovel.cli.commands.platform.load_settings", lambda _: settings)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "experience",
            "collect",
            source["id"],
            str(path),
            "--revision",
            str(source["revision"]),
        ],
    )
    assert result.exit_code == 0, result.output

    async def read_candidate() -> dict:
        async with baseline_runtime(settings) as current:
            result = await current.platform.execute(PlatformCommand(action="experience.list"), human=True)
            assert len(result["items"]) == 1
            return result["items"][0]

    candidate = asyncio.run(read_candidate())
    identifier = candidate["id"]
    for arguments in (
        ["experience", "list"],
        ["experience", "show", identifier],
        [
            "experience",
            "review",
            identifier,
            "--revision",
            "1",
            "--decision",
            "retain",
            "--note",
            "Human check only.",
        ],
        ["experience", "forget", identifier, "--revision", "2"],
    ):
        response = runner.invoke(app, arguments)
        assert response.exit_code == 0, response.output
    assert '"state": "deleted"' in response.output
