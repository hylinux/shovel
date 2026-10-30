from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable

from pydantic import JsonValue

from shovel.automation.conditions import ConditionEvaluator, required_value
from shovel.automation.domain import (
    AutomationControl,
    EventTrigger,
    NormalizedEvent,
    ProactiveConfiguration,
    ProactiveService,
    ScheduleTrigger,
    TargetCapability,
)
from shovel.automation.ports import ProactiveRepository
from shovel.automation.schedules import occurrence_key
from shovel.capability.domain import CapabilityReference, CapabilityScope
from shovel.domain.safe_data import ensure_no_credentials
from shovel.execution.domain import Execution, Invocation, InvocationContext
from shovel.execution.invocation import InvocationService
from shovel.runtime.errors import RuntimeFailure


def _key(service_id: str, event: NormalizedEvent) -> str:
    return (
        "proactive:" + hashlib.sha256(json.dumps([service_id, event.source, event.id]).encode()).hexdigest()
    )


def _event_hash(event: NormalizedEvent) -> str:
    data = event.model_dump(mode="json")
    if event.source == "manual" and event.event_type == "shovel.manual":
        data.pop("occurred_at")
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ProactiveServiceManager:
    """Match configuration and invoke; only the committed Execution is durable."""

    def __init__(
        self,
        repository: ProactiveRepository,
        invocations: InvocationService,
        *,
        clock: Callable[[], float] = time.time,
        wake: Callable[[], None] | None = None,
    ) -> None:
        self.repository, self.invocations, self.clock, self.wake = repository, invocations, clock, wake
        self.conditions = ConditionEvaluator()
        self._admission = asyncio.Lock()

    async def resolve_target(self, scope: CapabilityScope, target: TargetCapability) -> CapabilityReference:
        registry = self.invocations.registry
        if target.version_policy == "latest_published":
            version = await registry.resolve_latest(scope, target.capability_id)
        else:
            assert target.version is not None
            version = await registry.resolve_exact(scope, target.capability_id, target.version)
        return CapabilityReference(capability_id=version.capability_id, version=version.number)

    async def create(
        self,
        scope: CapabilityScope,
        configuration: ProactiveConfiguration,
        *,
        enabled: bool = False,
    ) -> ProactiveService:
        ensure_no_credentials(configuration)
        configuration = ProactiveConfiguration.model_validate(configuration.model_dump(mode="json"))
        await self.resolve_target(scope, configuration.target)
        async with self._admission:
            return await self.repository.create(scope, configuration, enabled=enabled)

    async def create_draft(
        self,
        scope: CapabilityScope,
        configuration: ProactiveConfiguration,
        *,
        name: str = "",
        description: str = "",
    ) -> ProactiveService:
        ensure_no_credentials(configuration)
        target = configuration.target
        await self.invocations.registry.get(scope, target.capability_id)
        if target.version is not None:
            await self.invocations.registry.get_version(scope, target.capability_id, target.version)
        async with self._admission:
            return await self.repository.create(
                scope,
                configuration,
                enabled=False,
                name=name,
                description=description,
            )

    async def update(
        self,
        scope: CapabilityScope,
        identifier: str,
        configuration: ProactiveConfiguration,
        *,
        enabled: bool,
        expected_revision: int,
        name: str | None = None,
        description: str | None = None,
        paused: bool | None = None,
    ) -> ProactiveService:
        ensure_no_credentials(configuration)
        configuration = ProactiveConfiguration.model_validate(configuration.model_dump(mode="json"))
        if enabled:
            await self.resolve_target(scope, configuration.target)
        async with self._admission:
            return await self.repository.update(
                scope,
                identifier,
                configuration,
                enabled=enabled,
                expected_revision=expected_revision,
                name=name,
                description=description,
                paused=paused,
            )

    async def get(self, scope: CapabilityScope, identifier: str) -> ProactiveService:
        return await self.repository.get(scope, identifier)

    async def list(self, scope: CapabilityScope) -> list[ProactiveService]:
        return await self.repository.list(scope)

    async def control(self, scope: CapabilityScope) -> AutomationControl:
        return await self.repository.control(scope)

    async def set_paused(
        self, scope: CapabilityScope, *, paused: bool, expected_revision: int
    ) -> AutomationControl:
        async with self._admission:
            return await self.repository.set_paused(scope, paused=paused, expected_revision=expected_revision)

    async def find_run(self, scope: CapabilityScope, identifier: str, request_key: str) -> Execution | None:
        event = NormalizedEvent(
            id="manual_" + hashlib.sha256(request_key.encode()).hexdigest(),
            scope=scope,
            event_type="shovel.manual",
            source="manual",
            occurred_at=0,
        )
        return await self.invocations.find_request(scope, _key(identifier, event))

    async def admission_reason(
        self,
        service: ProactiveService,
        event: NormalizedEvent | None,
        *,
        manual: bool,
    ) -> str | None:
        if not service.enabled:
            return "disabled"
        if service.paused:
            return "paused"
        config, now = service.configuration, self.clock()
        if not manual:
            control = await self.control(service.scope)
            if control.paused:
                return "globally_paused"
            if event is not None and control.resumed_at > 0 and event.occurred_at <= control.resumed_at:
                return "before_global_resume"
            if config.quiet_hours is not None and config.quiet_hours.contains(now):
                return "quiet_hours"
            last = await self.repository.last_invoked(service)
            if last is not None and now - last < config.cooldown_seconds:
                return "cooldown"
        if config.conditions:
            if event is None:
                return "event_payload_required"
            data: dict[str, JsonValue] = event.model_dump(mode="json")
            if not all(self.conditions.matches(condition, data) for condition in config.conditions):
                return "condition_mismatch"
        return None

    def _matches(self, service: ProactiveService, event: NormalizedEvent) -> bool:
        trigger = service.configuration.trigger
        if service.scope != event.scope:
            return False
        if isinstance(trigger, EventTrigger):
            return (
                event.schedule is None
                and trigger.event_type == event.event_type
                and (trigger.source is None or trigger.source == event.source)
            )
        if isinstance(trigger, ScheduleTrigger):
            occurrence = event.schedule
            return (
                occurrence is not None
                and occurrence.service_id == service.id
                and occurrence.service_revision == service.revision
                and event.id == occurrence_key(service.id, service.revision, occurrence.scheduled_at)
            )
        return False

    async def handle_event(self, event: NormalizedEvent) -> None:
        import structlog

        for service in await self.repository.list(event.scope):
            try:
                await self.dispatch(service.scope, service.id, event)
            except Exception as exc:
                structlog.get_logger("shovel.proactive").error(
                    "proactive.admission_failed",
                    service_id=service.id,
                    event_id=event.id,
                    error_type=type(exc).__name__,
                )

    async def dispatch(
        self,
        scope: CapabilityScope,
        identifier: str,
        event: NormalizedEvent,
    ) -> Execution | None:
        ensure_no_credentials(event)
        event = NormalizedEvent.model_validate(event.model_dump(mode="json"))
        async with self._admission:
            service = await self.repository.get(scope, identifier)
            if not self._matches(service, event):
                return None
            return await self._invoke(service, event, manual=False)

    async def run_now(
        self,
        scope: CapabilityScope,
        identifier: str,
        *,
        request_key: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> Execution:
        if not request_key or len(request_key) > 128:
            raise RuntimeFailure("Manual run requires a stable request key of 1-128 characters.")
        event_id = "manual_" + hashlib.sha256(request_key.encode()).hexdigest()
        async with self._admission:
            service = await self.repository.get(scope, identifier)
            execution = await self._invoke(
                service,
                NormalizedEvent(
                    id=event_id,
                    scope=scope,
                    event_type="shovel.manual",
                    source="manual",
                    occurred_at=self.clock(),
                    payload=payload if payload is not None else {},
                ),
                manual=True,
            )
            if execution is None:
                raise RuntimeFailure(
                    "Manual run requires an enabled service and matching conditions.", status=409
                )
            return execution

    async def _invoke(
        self, service: ProactiveService, event: NormalizedEvent, *, manual: bool
    ) -> Execution | None:
        key = _key(service.id, event)
        previous = await self.invocations.find_request(service.scope, key)
        if previous is not None:
            previous_snapshot = previous.snapshot.context.proactive_snapshot
            if previous_snapshot is not None and previous_snapshot["event_hash"] != _event_hash(event):
                raise RuntimeFailure("Event identity conflicts with persisted event content.", status=409)
            return previous
        if await self.admission_reason(service, event, manual=manual) is not None:
            return None
        config, now = service.configuration, self.clock()
        data: dict[str, JsonValue] = event.model_dump(mode="json")
        payload = {
            key: binding.value if binding.source == "literal" else required_value(data, binding.path)
            for key, binding in config.input_mapping.items()
        }
        snapshot: dict[str, JsonValue] = service.model_dump(mode="json")
        snapshot["event_hash"] = _event_hash(event)
        snapshot["event_id"] = event.id
        snapshot["event_type"] = event.event_type
        snapshot["event_source"] = event.source
        snapshot["occurred_at"] = event.occurred_at
        snapshot["admitted_at"] = now
        execution = await self.invocations.invoke(
            Invocation(
                capability=await self.resolve_target(service.scope, config.target),
                context=InvocationContext(
                    scope=service.scope,
                    actor_id=service.scope.user_id,
                    source="proactive",
                    permission_ceiling=config.permission_boundary,
                    proactive_boundary=config.permission_boundary,
                    proactive_preference=config.approval_preference,
                    proactive_snapshot=snapshot,
                ),
                input=payload,
                request_key=key,
            )
        )
        if self.wake is not None:
            self.wake()
        return execution
