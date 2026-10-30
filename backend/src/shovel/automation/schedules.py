from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta

import structlog

from shovel.automation.domain import (
    NormalizedEvent,
    ScheduleDefinition,
    ScheduleOccurrence,
    ScheduleTrigger,
    timezone,
)
from shovel.automation.ports import EventBus, ProactiveRepository


def occurrence_key(service_id: str, revision: int, scheduled_at: float) -> str:
    value = f"{service_id}:{revision}:{scheduled_at:.6f}"
    return "schedule_" + hashlib.sha256(value.encode()).hexdigest()


def latest_occurrence(schedule: ScheduleDefinition, after: float, through: float) -> float | None:
    if through <= after:
        return None

    if schedule.kind == "interval":
        assert schedule.interval_seconds is not None
        offset = math.floor((through - schedule.anchor_at) / schedule.interval_seconds)
        candidate = schedule.anchor_at + offset * schedule.interval_seconds
        return candidate if offset >= 0 and after < candidate <= through else None
    zone = timezone(schedule.timezone)
    day = datetime.fromtimestamp(through, zone).date()
    assert schedule.hour is not None and schedule.minute is not None
    for offset in range(8):
        local = datetime.combine(day - timedelta(days=offset), datetime.min.time()).replace(
            hour=schedule.hour,
            minute=schedule.minute,
            tzinfo=zone,
            fold=0,
        )
        utc = local.astimezone(UTC)
        # Reject nonexistent local times; fold=0 chooses the first repeated time.
        if utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            continue
        candidate = utc.timestamp()
        if candidate <= after:
            return None
        if candidate <= through:
            return candidate
    return None


def next_occurrence(schedule: ScheduleDefinition, after: float) -> float | None:
    if schedule.kind == "interval":
        assert schedule.interval_seconds is not None
        offset = max(0, math.floor((after - schedule.anchor_at) / schedule.interval_seconds) + 1)
        return schedule.anchor_at + offset * schedule.interval_seconds
    zone = timezone(schedule.timezone)
    day = datetime.fromtimestamp(after, zone).date()
    assert schedule.hour is not None and schedule.minute is not None
    for offset in range(8):
        local = datetime.combine(day + timedelta(days=offset), datetime.min.time()).replace(
            hour=schedule.hour,
            minute=schedule.minute,
            tzinfo=zone,
            fold=0,
        )
        utc = local.astimezone(UTC)
        if utc.timestamp() > after and utc.astimezone(zone).replace(tzinfo=None) == local.replace(
            tzinfo=None
        ):
            return utc.timestamp()
    return None


class ScheduleTriggerService:
    """Cursor records event admission, not durable delivery or execution completion."""

    def __init__(self, repository: ProactiveRepository, bus: EventBus) -> None:
        self.repository, self.bus = repository, bus
        self._log = structlog.get_logger("shovel.schedules")

    async def tick(self, now: float) -> None:
        for service in await self.repository.list():
            trigger = service.configuration.trigger
            if not service.enabled or service.paused or not isinstance(trigger, ScheduleTrigger):
                continue
            try:
                cursor = await self.repository.cursor(service)
                due = latest_occurrence(trigger.schedule, cursor, now)
                if due is not None and (
                    trigger.schedule.missed_policy == "fire_once"
                    or now - due <= trigger.schedule.misfire_grace_seconds
                ):
                    await self.bus.publish(
                        NormalizedEvent(
                            id=occurrence_key(service.id, service.revision, due),
                            scope=service.scope,
                            event_type="shovel.schedule",
                            source="schedule",
                            occurred_at=due,
                            schedule=ScheduleOccurrence(
                                service_id=service.id,
                                service_revision=service.revision,
                                scheduled_at=due,
                            ),
                        )
                    )
                await self.repository.checkpoint(service, now)
            except Exception as exc:
                self._log.error("schedule.failed", service_id=service.id, error_type=type(exc).__name__)
