from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
    model_validator,
)

from shovel.domain.safe_data import ensure_no_credentials

type Identifier = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]
type NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32000)]
type Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def credential_free(cls, value: object) -> object:
        ensure_no_credentials(value)
        return value


def object_schema() -> dict[str, JsonValue]:
    return {"type": "object"}


class CapabilityKind(StrEnum):
    FUNCTION = "function"
    SKILL = "skill"
    WORKFLOW = "workflow"
    AGENT = "agent"


class Availability(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class CapabilityState(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class CapabilityScope(Value):
    workspace_id: NonEmptyText
    user_id: NonEmptyText


class CapabilityReference(Value):
    capability_id: Identifier
    version: int = Field(ge=1, strict=True)


class LockedDependency(CapabilityReference):
    content_hash: Digest


class DependencyLock(Value):
    format_version: Literal[1] = 1
    dependencies: tuple[LockedDependency, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def unique_references(self) -> DependencyLock:
        keys = [(item.capability_id, item.version) for item in self.dependencies]
        if len(keys) != len(set(keys)):
            raise ValueError("Dependency lock contains duplicate exact versions.")
        return self


class Permission(Value):
    action: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    resource: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class RiskProfile(Value):
    level: Literal["low", "medium", "high", "critical"] = "low"
    reasons: tuple[NonEmptyText, ...] = ()


class SideEffectProfile(Value):
    kind: Literal["none", "local_write", "external_write", "destructive"] = "none"
    description: str = Field(default="", max_length=2000)


class ConnectionRequirement(Value):
    name: Identifier
    connection_ref: Identifier


class FunctionImplementation(Value):
    kind: Literal["function"] = "function"
    implementation_id: Identifier


class SkillImplementation(Value):
    kind: Literal["skill"] = "skill"
    instructions: NonEmptyText


class AgentImplementation(Value):
    kind: Literal["agent"] = "agent"
    instructions: NonEmptyText


class Condition(Value):
    path: tuple[NonEmptyText, ...] = Field(min_length=1, max_length=16)
    operator: Literal["eq", "ne", "contains", "in", "exists", "gt", "gte", "lt", "lte"]
    value: JsonValue = None


class InputBinding(Value):
    source: Literal["event", "context", "literal"] = "event"
    path: tuple[NonEmptyText, ...] = Field(default=(), max_length=16)
    value: JsonValue = None

    @model_validator(mode="after")
    def valid_binding(self) -> InputBinding:
        if (self.source != "literal") != bool(self.path) or (
            self.source != "literal" and self.value is not None
        ):
            raise ValueError("Reference bindings require a path; literal bindings require no path.")
        return self


class WorkflowNode(Value):
    id: Identifier
    kind: Literal[
        "input",
        "function",
        "skill",
        "condition",
        "approval",
        "ask_user",
        "notification",
        "emit_event",
        "output",
    ]
    capability: CapabilityReference | None = None
    input_mapping: dict[str, InputBinding] = Field(default_factory=dict)
    condition: Condition | None = None
    permission_boundary: tuple[Permission, ...] | None = None
    prompt: str = Field(default="", max_length=4000)
    response_schema: dict[str, JsonValue] = Field(default_factory=object_schema)
    event_type: NonEmptyText | None = None

    @model_serializer(mode="wrap")
    def compatible_dump(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        defaults: dict[str, object] = {
            "input_mapping": {},
            "condition": None,
            "permission_boundary": None,
            "prompt": "",
            "response_schema": object_schema(),
            "event_type": None,
        }
        for field, default in defaults.items():
            if field in data and data[field] == default:
                data.pop(field)
        return data


class WorkflowEdge(Value):
    source: Identifier
    target: Identifier
    label: Literal["next", "true", "false", "error"] = "next"

    @model_serializer(mode="wrap")
    def compatible_dump(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if data.get("label") == "next":
            data.pop("label")
        return data


class WorkflowImplementation(Value):
    kind: Literal["workflow"] = "workflow"
    nodes: tuple[WorkflowNode, ...] = Field(max_length=256)
    edges: tuple[WorkflowEdge, ...] = Field(default=(), max_length=512)


type Implementation = Annotated[
    FunctionImplementation | SkillImplementation | WorkflowImplementation | AgentImplementation,
    Field(discriminator="kind"),
]


class CapabilityContent(Value):
    implementation: Implementation
    input_schema: dict[str, JsonValue] = Field(default_factory=object_schema)
    output_schema: dict[str, JsonValue] = Field(default_factory=object_schema)
    required_permissions: tuple[Permission, ...] = ()
    permission_boundary: tuple[Permission, ...] = ()
    risk_profile: RiskProfile = Field(default_factory=RiskProfile)
    side_effect_profile: SideEffectProfile = Field(default_factory=SideEffectProfile)
    approval_requirement: Literal["platform_minimum", "always"] = "platform_minimum"
    allowed_capabilities: tuple[CapabilityReference, ...] = Field(default=(), max_length=256)
    dependency_lock: DependencyLock = Field(default_factory=DependencyLock)
    connection_requirements: tuple[ConnectionRequirement, ...] = ()
    provenance: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_content(self) -> CapabilityContent:
        encoded = json.dumps(self.model_dump(mode="json"), allow_nan=False).encode("utf-8")
        if len(encoded) > 262144:
            raise ValueError("Capability content exceeds the 256 KiB limit.")
        return self

    @property
    def content_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class FunctionContract(Value):
    """Metadata supplied by trusted platform code, never a module path or callable."""

    implementation_id: Identifier
    retry_safe: bool = False
    input_schema: dict[str, JsonValue] = Field(default_factory=object_schema)
    output_schema: dict[str, JsonValue] = Field(default_factory=object_schema)
    required_permissions: tuple[Permission, ...] = ()
    risk_profile: RiskProfile = Field(default_factory=RiskProfile)
    side_effect_profile: SideEffectProfile = Field(default_factory=SideEffectProfile)
    approval_requirement: Literal["platform_minimum", "always"] = "platform_minimum"
    connection_requirements: tuple[ConnectionRequirement, ...] = ()


class CapabilityDefinition(Value):
    id: str
    scope: CapabilityScope
    kind: CapabilityKind
    name: str
    description: str
    availability: Availability
    created_at: int


class CapabilityVersion(Value):
    id: str
    capability_id: str
    number: int
    revision: int
    content: CapabilityContent
    availability: Availability
    created_at: int
    published_at: int | None
    published_by: str | None
    content_hash: str | None
    based_on_version_id: str | None

    @property
    def state(self) -> CapabilityState:
        if self.availability != Availability.ACTIVE:
            return CapabilityState(self.availability.value)
        return CapabilityState.PUBLISHED if self.published_at is not None else CapabilityState.DRAFT


class ValidationResult(Value):
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors
