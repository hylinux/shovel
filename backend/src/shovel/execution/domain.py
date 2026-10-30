from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import Field, JsonValue, StringConstraints, model_validator

from shovel.capability.domain import (
    CapabilityContent,
    CapabilityKind,
    CapabilityReference,
    CapabilityScope,
    Digest,
    Identifier,
    NonEmptyText,
    Permission,
    Value,
)
from shovel.runtime.errors import RuntimeFailure
from shovel.security.domain import ApprovalPreference

EXECUTION_KIND = "capability.invoke"


class ExecutionState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUSPENDED = "suspended"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


class ExecutionStateMachine:
    _transitions: ClassVar[dict[ExecutionState, set[ExecutionState]]] = {
        ExecutionState.QUEUED: {
            ExecutionState.RUNNING,
            ExecutionState.SUSPENDED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
        },
        ExecutionState.RUNNING: {
            ExecutionState.SUSPENDED,
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
        },
        ExecutionState.SUSPENDED: {ExecutionState.QUEUED, ExecutionState.FAILED, ExecutionState.CANCELLED},
        ExecutionState.SUCCEEDED: set(),
        ExecutionState.FAILED: set(),
        ExecutionState.CANCELLED: set(),
    }

    @classmethod
    def targets(cls, current: ExecutionState) -> frozenset[ExecutionState]:
        return frozenset(cls._transitions[current])

    @classmethod
    def check(cls, current: ExecutionState, target: ExecutionState) -> None:
        if target not in cls._transitions[current]:
            raise RuntimeFailure(
                f"Illegal execution transition: {current.value} -> {target.value}.", status=409
            )


class BudgetExceeded(RuntimeFailure):
    def __init__(self, counter: str) -> None:
        super().__init__(f"Execution budget exceeded: {counter}.", status=409)


class FrozenVersionUnavailable(RuntimeFailure):
    pass


class ExecutionBudget(Value):
    max_duration_seconds: float = Field(default=300, gt=0, le=86400)
    max_steps: int = Field(default=100, ge=1, le=10000, strict=True)
    max_child_executions: int = Field(default=20, ge=0, le=1000, strict=True)
    max_call_depth: int = Field(default=8, ge=1, le=32, strict=True)
    max_agent_turns: int = Field(default=10, ge=0, le=1000, strict=True)
    max_function_calls: int = Field(default=50, ge=0, le=10000, strict=True)

    def narrows(self, parent: ExecutionBudget) -> bool:
        return all(getattr(self, name) <= getattr(parent, name) for name in type(self).model_fields)


class BudgetUsage(Value):
    steps: int = Field(default=0, ge=0, strict=True)
    child_executions: int = Field(default=0, ge=0, strict=True)
    agent_turns: int = Field(default=0, ge=0, strict=True)
    function_calls: int = Field(default=0, ge=0, strict=True)

    def charge(self, delta: BudgetUsage, budget: ExecutionBudget) -> BudgetUsage:
        values = {name: getattr(self, name) + getattr(delta, name) for name in type(self).model_fields}
        for name, value in values.items():
            if value > getattr(budget, f"max_{name}"):
                raise BudgetExceeded(name)
        return BudgetUsage.model_validate(values)


class InvocationContext(Value):
    scope: CapabilityScope
    actor_id: NonEmptyText
    source: Literal["manual", "agent", "child", "proactive"] = "manual"
    correlation_id: Identifier | None = None
    permission_ceiling: tuple[Permission, ...] = ()
    proactive_boundary: tuple[Permission, ...] | None = None
    workflow_boundary: tuple[Permission, ...] | None = None
    skill_boundary: tuple[Permission, ...] | None = None
    workflow_approval_required: bool = False
    proactive_preference: ApprovalPreference | None = None
    proactive_snapshot: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def bounded_provenance(self) -> InvocationContext:
        if self.proactive_snapshot is not None:
            bounded_json(self.proactive_snapshot)
        return self


def bounded_json(value: JsonValue) -> None:
    if len(json.dumps(value, allow_nan=False).encode("utf-8")) > 262144:
        raise ValueError("Execution data exceeds the 256 KiB limit.")


class Invocation(Value):
    capability: CapabilityReference
    context: InvocationContext
    input: dict[str, JsonValue] = Field(default_factory=dict)
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    request_key: (
        Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")] | None
    ) = None

    @model_validator(mode="after")
    def bounded_input(self) -> Invocation:
        bounded_json(self.input)
        if self.context.source == "child":
            raise ValueError("Child invocations require a running parent execution lease.")
        return self


class ChildInvocation(Value):
    capability: CapabilityReference
    call_key: Identifier
    input: dict[str, JsonValue] = Field(default_factory=dict)
    budget: ExecutionBudget | None = None
    permission_ceiling: tuple[Permission, ...] | None = None
    workflow_approval_required: bool = False

    @model_validator(mode="after")
    def bounded_input(self) -> ChildInvocation:
        bounded_json(self.input)
        return self


class CapabilitySnapshot(Value):
    capability_id: str
    version_id: str
    number: int
    kind: CapabilityKind
    content_hash: Digest
    content: CapabilityContent

    @model_validator(mode="after")
    def valid_content(self) -> CapabilitySnapshot:
        if (
            self.content_hash != self.content.content_hash
            or self.content.implementation.kind != self.kind.value
        ):
            raise ValueError("Frozen capability content does not match its identity.")
        return self


class ExecutionSnapshot(Value):
    capability: CapabilitySnapshot
    context: InvocationContext
    budget: ExecutionBudget
    capability_call_path: tuple[CapabilityReference, ...]
    granted_permissions: tuple[Permission, ...] = ()

    @model_validator(mode="after")
    def valid_call_path(self) -> ExecutionSnapshot:
        identifiers = [item.capability_id for item in self.capability_call_path]
        expected = CapabilityReference(
            capability_id=self.capability.capability_id, version=self.capability.number
        )
        if (
            not self.capability_call_path
            or self.capability_call_path[-1] != expected
            or len(identifiers) != len(set(identifiers))
            or len(identifiers) > self.budget.max_call_depth
        ):
            raise ValueError("Frozen execution call path is invalid.")
        return self


class Execution(Value):
    id: str
    root_execution_id: str
    parent_execution_id: str | None
    correlation_id: str
    child_key: str | None
    snapshot: ExecutionSnapshot
    input: dict[str, JsonValue]
    state: ExecutionState
    usage: BudgetUsage
    output: JsonValue = None
    error: str | None = None
    revision: int
    attempts: int
    created_at: int
    deadline_at: float | None = None
    approval_request_id: str | None = None
    checkpoint: dict[str, JsonValue] | None = None
    control: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def wait_reason(self) -> str | None:
        if self.error == "approval_pending":
            return "waiting_for_approval"
        value = self.checkpoint.get("wait_reason") if self.checkpoint else None
        if isinstance(value, str):
            return value
        reason = self.control.get("wait_reason")
        return reason if isinstance(reason, str) else None


class ExecutionLease(Value):
    execution: Execution
    token: str


class ExecutionAttempt(Value):
    execution_id: str
    number: int
    started_at: float
    dispatched_at: float | None
    finished_at: float | None
    outcome: ExecutionState | None
    reason: str | None = None


class ExecutionStep(Value):
    execution_id: str
    attempt_number: int
    state: ExecutionState
    started_at: float
    finished_at: float | None


class ExecutorResult(Value):
    output: JsonValue = None
    suspended: bool = False
    wake_at: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def bounded_output(self) -> ExecutorResult:
        bounded_json(self.output)
        if self.wake_at is not None and not self.suspended:
            raise ValueError("A wake_at result must suspend the Execution.")
        return self
