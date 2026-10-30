from typing import Literal

from pydantic import Field, JsonValue, model_validator

from shovel.capability.domain import CapabilityVersion, Identifier, Value, WorkflowImplementation
from shovel.execution.domain import bounded_json

# Graph definitions and immutable versions reuse the Capability plane, not parallel tables.
type WorkflowDefinition = WorkflowImplementation
type WorkflowVersion = CapabilityVersion


class WorkflowCheckpoint(Value):
    node_id: Identifier
    values: dict[str, JsonValue] = Field(default_factory=dict)
    charged: bool = False
    wait_reason: Literal["waiting_for_approval", "waiting_for_input", "waiting_for_child"] | None = None
    child_execution_id: Identifier | None = None

    @model_validator(mode="after")
    def bounded(self) -> WorkflowCheckpoint:
        bounded_json(self.model_dump(mode="json"))
        return self


class InputRequest(Value):
    id: Identifier
    execution_id: Identifier
    user_id: str
    node_id: Identifier
    state: Literal["pending", "answered", "consumed", "expired"]
    response: JsonValue = None
    prompt: str
    response_schema: dict[str, JsonValue]
    expires_at: float
