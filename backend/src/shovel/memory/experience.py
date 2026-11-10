"""Explicit, output-checked experience candidates; never an activation or execution path."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Protocol

from pydantic import Field, JsonValue, StringConstraints, model_validator

from shovel.capability.domain import CapabilityScope, Condition, Value, WorkflowImplementation
from shovel.domain.conditions import ConditionEvaluator, required_value
from shovel.execution.domain import Execution, ExecutionState
from shovel.memory.domain import _SECRET
from shovel.protection.egress import EgressChannel, require_ordinary
from shovel.runtime.diagnostics import trace
from shovel.runtime.errors import RuntimeFailure

Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
PathPart = Annotated[str, StringConstraints(min_length=1, max_length=128)]
VERIFIER_VERSION = "workflow-output-checks-v1"


def digest(value: JsonValue) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def source_digest(execution: Execution) -> str:
    return digest(execution.model_dump(mode="json"))


class OutputCheck(Value):
    path: tuple[PathPart, ...] = Field(min_length=1, max_length=16)
    expected: JsonValue


class ExperienceSubmission(Value):
    problem: Note
    environment: tuple[Note, ...] = Field(min_length=1, max_length=8)
    applicability: tuple[Note, ...] = Field(min_length=1, max_length=8)
    checks: tuple[OutputCheck, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def bounded(self) -> ExperienceSubmission:
        serialized = self.model_dump_json()
        if len(serialized) > 12000:
            raise ValueError("Experience submission exceeds the 12000 character limit.")
        if _SECRET.search(serialized):
            raise ValueError("Credentials cannot be saved as experience.")
        if len({check.path for check in self.checks}) != len(self.checks):
            raise ValueError("Output check paths must be unique.")
        return self


class ExperienceReview(Value):
    decision: Literal["retain", "reject"]
    note: Note

    @model_validator(mode="after")
    def safe_note(self) -> ExperienceReview:
        if _SECRET.search(self.note):
            raise ValueError("Credentials cannot be saved in review notes.")
        return self


def evidence(execution: Execution, submission: ExperienceSubmission) -> dict[str, JsonValue]:
    require_ordinary(EgressChannel.MEMORY, (execution, submission))
    content = execution.snapshot.capability.content
    graph = content.implementation
    if (
        execution.state != ExecutionState.SUCCEEDED
        or execution.parent_execution_id is not None
        or execution.snapshot.context.source != "manual"
        or execution.snapshot.enterprise_rollout_binding is not None
        or not isinstance(graph, WorkflowImplementation)
        or any(node.kind not in {"input", "condition", "output"} for node in graph.nodes)
        or content.side_effect_profile.kind != "none"
        or content.connection_requirements
        or content.required_permissions
        or content.dependency_lock.dependencies
    ):
        raise RuntimeFailure(
            "Experience intake accepts only completed, manual, pure Input/Condition/Output workflows.",
            status=409,
        )
    serialized = execution.model_dump_json()
    if _SECRET.search(serialized):
        raise RuntimeFailure("The source is not eligible for ordinary Memory persistence.", status=409)
    observations: list[JsonValue] = []
    evaluator = ConditionEvaluator()
    all_passed = True
    for check in submission.checks:
        exists = evaluator.matches(Condition(path=check.path, operator="exists"), execution.output)
        actual = required_value(execution.output, check.path) if exists else None
        passed = exists and digest(actual) == digest(check.expected)
        all_passed = all_passed and passed
        observations.append(
            {
                "path": list(check.path),
                "expected": check.expected,
                "exists": exists,
                "observed": actual,
                "passed": passed,
            }
        )
    completed = execution.checkpoint.get("values", {}) if execution.checkpoint else {}
    result: dict[str, JsonValue] = {
        "problem": submission.problem,
        "environment": list(submission.environment),
        "applicability": list(submission.applicability),
        "annotations_source": "human_unverified",
        "verifier_version": VERIFIER_VERSION,
        "source": {
            "execution_id": execution.id,
            "execution_revision": execution.revision,
            "capability_id": execution.snapshot.capability.capability_id,
            "capability_version": execution.snapshot.capability.number,
            "content_hash": execution.snapshot.capability.content_hash,
            "execution_digest": source_digest(execution),
        },
        "attempt": {
            "node_kinds": [node.kind for node in graph.nodes],
            "completed_node_ids": list(completed) if isinstance(completed, dict) else [],
            "execution_attempts": execution.attempts,
        },
        "observations": observations,
        "observed_outcome": "passed" if all_passed else "failed",
        "failure_reason": None if all_passed else "output_assertion_mismatch",
    }
    if len(json.dumps(result, ensure_ascii=False)) > 16000:
        raise RuntimeFailure("Experience evidence exceeds the 16000 character limit.", status=422)
    return result


class ExperienceRepository(Protocol):
    async def generation(self, scope: CapabilityScope) -> int: ...

    async def capture(
        self,
        scope: CapabilityScope,
        execution: Execution,
        submission_digest: str,
        data: dict[str, JsonValue],
        generation: int,
    ) -> dict[str, JsonValue]: ...

    async def list(self, scope: CapabilityScope) -> list[dict[str, JsonValue]]: ...

    async def get(self, scope: CapabilityScope, identifier: str) -> dict[str, JsonValue]: ...

    async def review(
        self, scope: CapabilityScope, identifier: str, revision: int, body: ExperienceReview
    ) -> dict[str, JsonValue]: ...

    async def forget(
        self, scope: CapabilityScope, identifier: str, revision: int
    ) -> dict[str, JsonValue]: ...


class ExperienceSources(Protocol):
    async def get(self, scope: CapabilityScope, identifier: str) -> Execution: ...


class ExperienceService:
    def __init__(self, repository: ExperienceRepository, sources: ExperienceSources) -> None:
        self.repository, self.sources = repository, sources

    async def collect(
        self,
        scope: CapabilityScope,
        execution_id: str,
        revision: int,
        submission: ExperienceSubmission,
    ) -> dict[str, JsonValue]:
        require_ordinary(EgressChannel.MEMORY, submission)
        generation = await self.repository.generation(scope)
        execution = await self.sources.get(scope, execution_id)
        if execution.revision != revision:
            raise RuntimeFailure("The source Execution has changed; reload it before collecting.", status=409)
        result = await self.repository.capture(
            scope,
            execution,
            digest(submission.model_dump(mode="json")),
            evidence(execution, submission),
            generation,
        )
        trace("memory.experience.collected", candidate_id=result["id"], state=result["state"])
        return result
