from typing import Literal

from pydantic import Field, JsonValue, model_validator

from shovel.automation.domain import ProactiveConfiguration
from shovel.capability.domain import CapabilityContent, CapabilityKind, Identifier, Permission, Value
from shovel.domain.safe_data import ensure_no_credentials
from shovel.execution.domain import ExecutionBudget, bounded_json

type Action = Literal[
    "capability.list",
    "capability.show",
    "capability.search",
    "capability.versions",
    "capability.create",
    "capability.update",
    "capability.fork",
    "capability.options",
    "capability.validate",
    "capability.publish",
    "execution.run",
    "execution.list",
    "execution.show",
    "execution.cancel",
    "execution.resume",
    "execution.find",
    "proactive.create",
    "proactive.update",
    "proactive.validate",
    "proactive.list",
    "proactive.show",
    "proactive.pause",
    "proactive.resume",
    "proactive.enable",
    "proactive.disable",
    "proactive.run",
    "proactive.history",
    "proactive.explain",
    "automation.show",
    "automation.pause",
    "automation.resume",
    "approval.list",
    "approval.show",
    "approval.approve",
    "approval.reject",
    "interaction.list",
    "interaction.show",
    "interaction.input",
]


class PlatformCommand(Value):
    action: Action
    identifier: Identifier | None = None
    kind: CapabilityKind | None = None
    version: int | None = Field(default=None, ge=1, strict=True)
    expected_revision: int | None = Field(default=None, ge=1, strict=True)
    execution_id: Identifier | None = None
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2000)
    query: str = Field(default="", max_length=500)
    content: CapabilityContent | None = None
    configuration: ProactiveConfiguration | None = None
    input: dict[str, JsonValue] = Field(default_factory=dict)
    response: JsonValue = None
    permissions: tuple[Permission, ...] | None = None
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    request_key: Identifier | None = None
    prepare: bool = False
    confirmation_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def bounded(self) -> PlatformCommand:
        bounded_json(self.model_dump(mode="json"))
        ensure_no_credentials(self)
        return self


class ExecutionTiming(Value):
    started_at: float | None = None
    finished_at: float | None = None
