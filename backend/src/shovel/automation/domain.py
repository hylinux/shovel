from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, JsonValue, model_validator

from shovel.capability.domain import CapabilityScope, Identifier, NonEmptyText, Permission, Value
from shovel.capability.domain import Condition as Condition
from shovel.capability.domain import InputBinding as InputBinding
from shovel.execution.domain import bounded_json
from shovel.security.domain import ApprovalPreference


def timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("An installed IANA timezone is required.") from exc


class ScheduleDefinition(Value):
    kind: Literal["interval", "daily"]
    timezone: NonEmptyText = "UTC"
    anchor_at: float = Field(default=0, ge=0, le=253402214400)
    interval_seconds: int | None = Field(default=None, ge=1, le=31536000)
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)
    missed_policy: Literal["skip", "fire_once"] = "skip"
    misfire_grace_seconds: int = Field(default=60, ge=0, le=86400)

    @model_validator(mode="after")
    def valid_schedule(self) -> ScheduleDefinition:
        timezone(self.timezone)
        if self.kind == "interval":
            valid = self.interval_seconds is not None and self.hour is None and self.minute is None
        else:
            valid = self.hour is not None and self.minute is not None and self.interval_seconds is None
        if not valid:
            raise ValueError("Schedule fields must match its kind.")
        return self


class ScheduleTrigger(Value):
    kind: Literal["schedule"] = "schedule"
    schedule: ScheduleDefinition


class EventTrigger(Value):
    kind: Literal["event"] = "event"
    event_type: NonEmptyText
    source: NonEmptyText | None = None


class ManualTrigger(Value):
    kind: Literal["manual"] = "manual"


class TargetCapability(Value):
    capability_id: Identifier
    version_policy: Literal["exact", "latest_published"] = "exact"
    version: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def valid_policy(self) -> TargetCapability:
        if (self.version_policy == "exact") != (self.version is not None):
            raise ValueError("Only exact version policies require a version number.")
        return self


class QuietHours(Value):
    timezone: NonEmptyText
    start_minute: int = Field(ge=0, le=1439)
    end_minute: int = Field(ge=0, le=1439)

    @model_validator(mode="after")
    def valid_hours(self) -> QuietHours:
        timezone(self.timezone)
        if self.start_minute == self.end_minute:
            raise ValueError("Quiet hours must not cover an ambiguous full day.")
        return self

    def contains(self, timestamp: float) -> bool:
        local = datetime.fromtimestamp(timestamp, timezone(self.timezone))
        minute = local.hour * 60 + local.minute
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute < self.end_minute
        return minute >= self.start_minute or minute < self.end_minute


class ProactiveConfiguration(Value):
    trigger: Annotated[ScheduleTrigger | EventTrigger | ManualTrigger, Field(discriminator="kind")]
    conditions: tuple[Condition, ...] = Field(default=(), max_length=32)
    target: TargetCapability
    input_mapping: dict[str, InputBinding] = Field(default_factory=dict, max_length=128)
    permission_boundary: tuple[Permission, ...] = ()
    approval_preference: ApprovalPreference = ApprovalPreference.MINIMUM_REQUIRED
    notification_policy: Literal["none", "failures", "all"] = "none"
    quiet_hours: QuietHours | None = None
    cooldown_seconds: float = Field(default=0, ge=0, le=31536000)

    @model_validator(mode="after")
    def bounded_configuration(self) -> ProactiveConfiguration:
        if any(binding.source == "context" for binding in self.input_mapping.values()):
            raise ValueError("Proactive mappings require event or literal sources.")
        bounded_json(self.model_dump(mode="json"))
        return self


class ProactiveService(Value):
    id: Identifier
    scope: CapabilityScope
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2000)
    enabled: bool = False
    paused: bool = False
    revision: int = Field(ge=1)
    configuration: ProactiveConfiguration
    created_at: float
    updated_at: float


class AutomationControl(Value):
    paused: bool = False
    revision: int = Field(default=1, ge=1)
    resumed_at: float = 0


class ScheduleOccurrence(Value):
    service_id: Identifier
    service_revision: int = Field(ge=1)
    scheduled_at: float = Field(ge=0, le=253402214400)


class NormalizedEvent(Value):
    id: Identifier
    scope: CapabilityScope
    event_type: NonEmptyText
    source: NonEmptyText
    occurred_at: float = Field(ge=0, le=253402214400)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    schedule: ScheduleOccurrence | None = None

    @model_validator(mode="after")
    def valid_event(self) -> NormalizedEvent:
        bounded_json(self.model_dump(mode="json"))
        if self.schedule is not None and (
            self.event_type != "shovel.schedule"
            or self.source != "schedule"
            or self.occurred_at != self.schedule.scheduled_at
        ):
            raise ValueError("Schedule occurrence must match the normalized event.")
        return self
