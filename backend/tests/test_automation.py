from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from shovel.automation.conditions import ConditionEvaluator
from shovel.automation.domain import (
    Condition,
    EventTrigger,
    InputBinding,
    ManualTrigger,
    NormalizedEvent,
    ProactiveConfiguration,
    QuietHours,
    ScheduleDefinition,
    ScheduleTrigger,
    TargetCapability,
)
from shovel.automation.events import InProcessEventBus
from shovel.automation.manager import ProactiveServiceManager
from shovel.automation.schedules import ScheduleTriggerService, latest_occurrence
from shovel.capability.domain import Availability, CapabilityScope
from shovel.db.migrations import upgrade_database
from shovel.db.proactive_repository import SQLiteProactiveRepository
from shovel.execution.domain import ExecutionState
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.hosting import AutomationWorker
from shovel.runtime.service import Runtime
from shovel.security.domain import ApprovalPreference, UserSecurityPolicy
from shovel.security.services import ApprovalResolver, RiskEvaluator
from test_capability_registry import SCOPE
from test_capability_registry import catalog as catalog
from test_execution_engine import Kernel, publish
from test_execution_engine import kernel as kernel
from test_hosted_runtime import runtime as runtime
from test_security_core import WRITE, SecureKernel, capability
from test_security_core import secured as secured


def event(identifier: str = "event", **kwargs: object) -> NormalizedEvent:
    return NormalizedEvent(
        id=identifier,
        scope=SCOPE,
        event_type="document.changed",
        source="test",
        occurred_at=1000,
        **kwargs,
    )


def stamp(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_multiple_handlers_fail_independently_and_receive_detached_events(cancelled: bool) -> None:
    bus = InProcessEventBus(concurrency=2, capacity=2)
    observed = []

    async def failing(value: NormalizedEvent) -> None:
        value.payload["modified"] = True
        if cancelled:
            raise asyncio.CancelledError
        raise ValueError("unlogged-event-body")

    async def receiving(value: NormalizedEvent) -> None:
        observed.append(value.payload)

    bus.subscribe(failing)
    bus.subscribe(receiving)
    await bus.start()
    try:
        await bus.publish(event(payload={"safe": True}))
        await asyncio.wait_for(bus.join(), timeout=1)
        assert observed == [{"safe": True}] and bus.failures == 1
    finally:
        await bus.stop()


async def test_event_concurrency_and_capacity_are_bounded() -> None:
    bus = InProcessEventBus(concurrency=2, capacity=1)
    release, started = asyncio.Event(), asyncio.Event()
    active = maximum = count = 0

    async def handler(value: NormalizedEvent) -> None:
        nonlocal active, maximum, count
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            started.set()
        try:
            await release.wait()
            count += 1
        finally:
            active -= 1

    bus.subscribe(handler)
    await bus.start()
    publishers = [asyncio.create_task(bus.publish(event(f"event-{number}"))) for number in range(6)]
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert maximum == 2 and any(not task.done() for task in publishers)
        release.set()
        await asyncio.gather(*publishers)
        await bus.join()
        assert count == 6 and maximum == 2
    finally:
        release.set()
        await asyncio.gather(*publishers, return_exceptions=True)
        await bus.stop()


async def test_stop_drains_accepted_events_and_releases_blocked_publishers() -> None:
    bus = InProcessEventBus(concurrency=1, capacity=1)
    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []

    async def handler(value: NormalizedEvent) -> None:
        entered.set()
        await release.wait()
        delivered.append(value.id)

    bus.subscribe(handler)
    with pytest.raises(RuntimeFailure):
        await bus.publish(event())
    await bus.start()
    await bus.publish(event("first"))
    await entered.wait()
    await bus.publish(event("second"))
    blocked = asyncio.create_task(bus.publish(event("third")))
    await asyncio.sleep(0)
    stopping = asyncio.create_task(bus.stop())
    with pytest.raises(RuntimeFailure, match=r"stopped|accepting"):
        await asyncio.wait_for(blocked, 1)
    assert not stopping.done()
    release.set()
    await asyncio.wait_for(stopping, 1)
    assert delivered == ["first", "second"]
    with pytest.raises(RuntimeFailure):
        await bus.publish(event("after"))
    await bus.start()
    await bus.publish(event("restart"))
    await bus.stop()
    assert delivered[-1] == "restart"


async def test_stop_timeout_cancels_stalled_handler() -> None:
    bus = InProcessEventBus(stop_timeout=0.02)
    entered, closed = asyncio.Event(), asyncio.Event()

    async def handler(value: NormalizedEvent) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    bus.subscribe(handler)
    await bus.start()
    await bus.publish(event())
    await entered.wait()
    with pytest.raises(TimeoutError):
        await bus.stop()
    assert closed.is_set()


@pytest.mark.parametrize(
    "operator,actual,expected,result",
    [
        ("eq", 2, 2, True),
        ("ne", 2, 3, True),
        ("contains", "abc", "b", True),
        ("contains", [1, 2], 2, True),
        ("in", "x", ["x", "y"], True),
        ("exists", None, None, True),
        ("gt", 3, 2, True),
        ("gte", 2, 2, True),
        ("lt", 1, 2, True),
        ("lte", 2, 2, True),
        ("gt", True, 0, False),
        ("eq", True, 1, False),
        ("contains", 2, 2, False),
    ],
)
def test_condition_operators(operator: str, actual: object, expected: object, result: bool) -> None:
    condition = Condition(path=("payload", "value"), operator=operator, value=expected)
    assert ConditionEvaluator().matches(condition, {"payload": {"value": actual}}) is result
    assert not ConditionEvaluator().matches(condition, {"payload": {}})


def test_invalid_schedule_and_condition_are_rejected() -> None:
    for data in (
        {"kind": "cron"},
        {"kind": "interval", "interval_seconds": 0},
        {"kind": "daily", "hour": 25, "minute": 0},
        {"kind": "daily", "hour": 2, "minute": 0, "timezone": "Not/AZone"},
        {"kind": "daily", "hour": 2, "minute": 0, "interval_seconds": 5},
    ):
        with pytest.raises(ValidationError):
            ScheduleDefinition.model_validate(data)
    with pytest.raises(ValidationError):
        Condition(path=("payload",), operator="eval", value="1+1")


def test_daily_dst_gap_is_skipped_and_fold_runs_only_once() -> None:
    spring = ScheduleDefinition(kind="daily", hour=2, minute=30, timezone="America/New_York")
    assert (
        latest_occurrence(spring, stamp("2026-03-08T00:00:00+00:00"), stamp("2026-03-09T01:00:00+00:00"))
        is None
    )
    assert latest_occurrence(
        spring, stamp("2026-03-08T00:00:00+00:00"), stamp("2026-03-09T07:00:00+00:00")
    ) == stamp("2026-03-09T06:30:00+00:00")
    fall = ScheduleDefinition(kind="daily", hour=1, minute=30, timezone="America/New_York")
    first = stamp("2026-11-01T05:30:00+00:00")
    assert latest_occurrence(fall, stamp("2026-11-01T00:00:00+00:00"), first) == first
    assert latest_occurrence(fall, first, stamp("2026-11-01T07:00:00+00:00")) is None


@dataclass
class Automation:
    kernel: Kernel
    repository: SQLiteProactiveRepository
    manager: ProactiveServiceManager
    bus: InProcessEventBus
    scheduler: ScheduleTriggerService
    configuration: ProactiveConfiguration


@pytest.fixture
async def automation(kernel: Kernel) -> AsyncIterator[Automation]:
    version = await publish(kernel.catalog)
    repository = SQLiteProactiveRepository(kernel.catalog.sessions, clock=lambda: kernel.now[0])
    manager = ProactiveServiceManager(repository, kernel.invocations, clock=lambda: kernel.now[0])
    bus = InProcessEventBus()
    bus.subscribe(manager.handle_event)
    await bus.start()
    try:
        yield Automation(
            kernel,
            repository,
            manager,
            bus,
            ScheduleTriggerService(repository, bus),
            ProactiveConfiguration(
                trigger=EventTrigger(event_type="document.changed"),
                target=TargetCapability(capability_id=version.capability_id, version=1),
            ),
        )
    finally:
        await bus.stop()


async def test_event_matching_conditions_mapping_scope_and_enabled(automation: Automation) -> None:
    config = automation.configuration.model_copy(
        update={
            "trigger": EventTrigger(event_type="document.changed", source="test"),
            "conditions": (Condition(path=("payload", "count"), operator="gt", value=2),),
            "input_mapping": {"count": InputBinding(path=("payload", "count"))},
        }
    )
    service = await automation.manager.create(SCOPE, config, enabled=True)
    assert await automation.manager.dispatch(SCOPE, service.id, event("low", payload={"count": 1})) is None
    assert (
        await automation.manager.dispatch(
            SCOPE, service.id, event("source").model_copy(update={"source": "other"})
        )
        is None
    )
    execution = await automation.manager.dispatch(SCOPE, service.id, event(payload={"count": 3}))
    assert execution is not None and execution.input == {"count": 3}
    assert execution.state == ExecutionState.QUEUED
    disabled = await automation.manager.update(SCOPE, service.id, config, enabled=False, expected_revision=1)
    assert disabled.revision == 2
    assert (
        await automation.manager.dispatch(SCOPE, service.id, event("disabled", payload={"count": 3})) is None
    )
    with pytest.raises(NotFound):
        await automation.repository.get(
            CapabilityScope(workspace_id="other", user_id=SCOPE.user_id), service.id
        )
    with pytest.raises(RuntimeFailure, match="revision conflict"):
        await automation.manager.update(SCOPE, service.id, config, enabled=True, expected_revision=1)
    assert execution.snapshot.context.proactive_snapshot["revision"] == 1


async def test_concurrent_duplicate_events_share_the_persisted_execution(automation: Automation) -> None:
    service = await automation.manager.create(SCOPE, automation.configuration, enabled=True)
    results = await asyncio.gather(
        *(automation.manager.dispatch(SCOPE, service.id, event()) for _ in range(6))
    )
    assert len({result.id for result in results}) == 1
    with pytest.raises(RuntimeFailure, match="identity conflicts"):
        await automation.manager.dispatch(SCOPE, service.id, event(payload={"changed": True}))


async def test_latest_policy_freezes_version_and_replay_survives_revision_changes(
    automation: Automation,
) -> None:
    config = automation.configuration.model_copy(
        update={
            "target": TargetCapability(
                capability_id=automation.configuration.target.capability_id, version_policy="latest_published"
            ),
        }
    )
    service = await automation.manager.create(SCOPE, config, enabled=True)
    first = await automation.manager.dispatch(SCOPE, service.id, event())
    catalog = automation.kernel.catalog
    draft = await catalog.service.create_draft_from_version(SCOPE, config.target.capability_id, 1)
    await catalog.service.publish(SCOPE, draft.capability_id, draft.number, expected_revision=draft.revision)
    await automation.manager.update(SCOPE, service.id, config, enabled=True, expected_revision=1)
    assert await automation.manager.dispatch(SCOPE, service.id, event()) == first
    second = await automation.manager.dispatch(SCOPE, service.id, event("next"))
    assert first.snapshot.capability.number == 1 and second.snapshot.capability.number == 2
    assert first.snapshot.context.proactive_snapshot["revision"] == 1
    assert second.snapshot.context.proactive_snapshot["revision"] == 2


async def test_latest_ignores_drafts_but_does_not_fall_back_from_disabled_versions(
    automation: Automation,
) -> None:
    config = automation.configuration.model_copy(
        update={
            "target": TargetCapability(
                capability_id=automation.configuration.target.capability_id, version_policy="latest_published"
            ),
        }
    )
    service = await automation.manager.create(SCOPE, config, enabled=True)
    catalog = automation.kernel.catalog
    draft = await catalog.service.create_draft_from_version(SCOPE, config.target.capability_id, 1)
    result = await automation.manager.dispatch(SCOPE, service.id, event("before-publish"))
    assert result.snapshot.capability.number == 1
    await catalog.service.publish(SCOPE, draft.capability_id, draft.number, expected_revision=draft.revision)
    await catalog.service.set_availability(SCOPE, draft.capability_id, Availability.DISABLED, number=2)
    with pytest.raises(RuntimeFailure, match="unavailable"):
        await automation.manager.dispatch(SCOPE, service.id, event("disabled-version"))
    disabled = await automation.manager.update(SCOPE, service.id, config, enabled=False, expected_revision=1)
    assert not disabled.enabled and await automation.manager.get(SCOPE, service.id) == disabled
    assert await automation.manager.list(SCOPE) == [disabled]


async def test_secrets_are_rejected_before_serializing_configuration_or_events(
    automation: Automation,
) -> None:
    binding = InputBinding(source="literal", value=None).model_copy(
        update={"value": SecretStr("private-value")}
    )
    unsafe = automation.configuration.model_copy(update={"input_mapping": {"value": binding}})
    with pytest.raises(ValueError, match="Credentials"):
        await automation.manager.create(SCOPE, unsafe)
    raw_event = event()
    raw_event.payload["value"] = SecretStr("private-value")
    with pytest.raises(ValueError, match="Credentials"):
        await automation.bus.publish(raw_event)
    assert await automation.manager.list(SCOPE) == []


async def test_manual_run_bypasses_quiet_and_cooldown_but_not_approval_or_disabled(
    automation: Automation,
) -> None:
    config = automation.configuration.model_copy(
        update={
            "trigger": ManualTrigger(),
            "cooldown_seconds": 1000,
            "quiet_hours": QuietHours(timezone="UTC", start_minute=0, end_minute=60),
            "approval_preference": ApprovalPreference.ALWAYS,
        }
    )
    service = await automation.manager.create(SCOPE, config, enabled=True)
    first = await automation.manager.run_now(SCOPE, service.id, request_key="manual-key")
    automation.kernel.now[0] += 5
    assert await automation.manager.run_now(SCOPE, service.id, request_key="manual-key") == first
    with pytest.raises(RuntimeFailure, match="identity conflicts"):
        await automation.manager.run_now(
            SCOPE, service.id, request_key="manual-key", payload={"changed": True}
        )
    result = await automation.kernel.engine.run(SCOPE, first.id)
    assert result.state == ExecutionState.SUSPENDED and result.approval_request_id
    await automation.manager.update(SCOPE, service.id, config, enabled=False, expected_revision=1)
    with pytest.raises(RuntimeFailure, match="enabled service"):
        await automation.manager.run_now(SCOPE, service.id, request_key="disabled-key")


async def test_quiet_hours_cooldown_and_restart(automation: Automation) -> None:
    config = automation.configuration.model_copy(update={"cooldown_seconds": 100})
    service = await automation.manager.create(SCOPE, config, enabled=True)
    assert await automation.manager.dispatch(SCOPE, service.id, event()) is not None
    restarted = ProactiveServiceManager(
        SQLiteProactiveRepository(automation.kernel.catalog.sessions, clock=lambda: automation.kernel.now[0]),
        automation.kernel.invocations,
        clock=lambda: automation.kernel.now[0],
    )
    assert await restarted.dispatch(SCOPE, service.id, event("cooldown")) is None
    automation.kernel.now[0] += 101
    assert await restarted.dispatch(SCOPE, service.id, event("after")) is not None
    quiet = config.model_copy(
        update={"quiet_hours": QuietHours(timezone="UTC", start_minute=1380, end_minute=60)}
    )
    await restarted.update(SCOPE, service.id, quiet, enabled=True, expected_revision=1)
    assert await restarted.dispatch(SCOPE, service.id, event("quiet")) is None


async def test_fractional_cooldown_uses_the_durable_admission_time(automation: Automation) -> None:
    config = automation.configuration.model_copy(update={"cooldown_seconds": 0.5})
    service = await automation.manager.create(SCOPE, config, enabled=True)
    automation.kernel.now[0] = 1000.9
    assert await automation.manager.dispatch(SCOPE, service.id, event()) is not None
    automation.kernel.now[0] = 1001.1
    assert await automation.manager.dispatch(SCOPE, service.id, event("too-early")) is None
    automation.kernel.now[0] = 1001.5
    assert await automation.manager.dispatch(SCOPE, service.id, event("ready")) is not None


@pytest.mark.parametrize("policy,expected", [("skip", 0), ("fire_once", 1)])
async def test_missed_schedule_restart_dedupe_and_disabled(
    automation: Automation,
    policy: str,
    expected: int,
) -> None:
    config = automation.configuration.model_copy(
        update={
            "trigger": ScheduleTrigger(
                schedule=ScheduleDefinition(
                    kind="interval",
                    interval_seconds=100,
                    missed_policy=policy,
                    misfire_grace_seconds=0,
                )
            ),
        }
    )
    service = await automation.manager.create(SCOPE, config, enabled=True)
    automation.kernel.now[0] = 1350
    scheduler = ScheduleTriggerService(automation.repository, automation.bus)
    await scheduler.tick(1350)
    await automation.bus.join()
    async with automation.kernel.catalog.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM operation")) == expected
    if expected:
        async with automation.kernel.catalog.sessions() as session:
            await session.execute(text("UPDATE proactive_service SET schedule_cursor=1000"))
            await session.commit()
        await scheduler.tick(1350)
        await automation.bus.join()
        async with automation.kernel.catalog.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM operation")) == 1
    await automation.manager.update(SCOPE, service.id, config, enabled=False, expected_revision=1)
    await scheduler.tick(1500)
    await automation.bus.join()
    async with automation.kernel.catalog.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM operation")) == expected


async def test_event_is_not_durable_until_invocation_commit(
    automation: Automation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await automation.manager.create(SCOPE, automation.configuration, enabled=True)
    invoke = automation.kernel.invocations.invoke
    monkeypatch.setattr(
        automation.kernel.invocations, "invoke", AsyncMock(side_effect=RuntimeFailure("injected failure"))
    )
    with pytest.raises(RuntimeFailure, match="injected"):
        await automation.manager.dispatch(SCOPE, service.id, event())
    async with automation.kernel.catalog.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM operation")) == 0
    monkeypatch.setattr(automation.kernel.invocations, "invoke", invoke)
    execution = await automation.manager.dispatch(SCOPE, service.id, event())
    await automation.bus.stop()
    assert await automation.kernel.repository.get(SCOPE, execution.id) == execution


async def test_schedule_only_publishes_and_stale_revisions_cannot_invoke(automation: Automation) -> None:
    config = automation.configuration.model_copy(
        update={
            "trigger": ScheduleTrigger(schedule=ScheduleDefinition(kind="interval", interval_seconds=100)),
        }
    )
    service = await automation.manager.create(SCOPE, config, enabled=True)
    observed = []

    async def capture(value: NormalizedEvent) -> None:
        observed.append(value)

    bus = InProcessEventBus()
    bus.subscribe(capture)
    await bus.start()
    try:
        await ScheduleTriggerService(automation.repository, bus).tick(1100)
        await bus.join()
    finally:
        await bus.stop()
    (occurrence,) = observed
    assert occurrence.schedule.scheduled_at == 1100
    async with automation.kernel.catalog.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM operation")) == 0
    await automation.manager.update(SCOPE, service.id, config, enabled=True, expected_revision=1)
    assert await automation.manager.dispatch(SCOPE, service.id, occurrence) is None


async def test_commit_then_lost_response_replays_without_resolving_a_new_version(
    automation: Automation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await automation.manager.create(SCOPE, automation.configuration, enabled=True)
    invoke = automation.kernel.invocations.invoke

    async def lost_response(invocation):
        await invoke(invocation)
        raise RuntimeFailure("lost response after commit")

    monkeypatch.setattr(automation.kernel.invocations, "invoke", lost_response)
    with pytest.raises(RuntimeFailure, match="after commit"):
        await automation.manager.dispatch(SCOPE, service.id, event())
    monkeypatch.setattr(
        automation.kernel.invocations.registry, "resolve_exact", AsyncMock(side_effect=AssertionError)
    )
    replayed = await automation.manager.dispatch(SCOPE, service.id, event())
    assert replayed is not None
    async with automation.kernel.catalog.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM operation")) == 1


async def test_sources_have_independent_event_identity_and_services_fail_independently(
    automation: Automation,
) -> None:
    bad = automation.configuration.model_copy(
        update={
            "input_mapping": {"absent": InputBinding(path=("payload", "absent"))},
        }
    )
    await automation.manager.create(SCOPE, bad, enabled=True)
    good = await automation.manager.create(SCOPE, automation.configuration, enabled=True)
    await automation.bus.publish(event())
    await automation.bus.publish(event().model_copy(update={"source": "another-source"}))
    await automation.bus.join()
    async with automation.kernel.catalog.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM operation")) == 2
    assert await automation.manager.dispatch(SCOPE, good.id, event()) is not None


async def test_proactive_boundary_and_approval_never_grant_missing_permissions(secured: SecureKernel) -> None:
    version = await capability(secured)
    repository = SQLiteProactiveRepository(secured.catalog.sessions, clock=lambda: secured.now[0])
    manager = ProactiveServiceManager(repository, secured.invocations, clock=lambda: secured.now[0])
    config = ProactiveConfiguration(
        trigger=ManualTrigger(),
        target=TargetCapability(capability_id=version.capability_id, version=1),
        permission_boundary=(WRITE,),
        approval_preference=ApprovalPreference.ALWAYS,
    )
    service = await manager.create(SCOPE, config, enabled=True)
    with pytest.raises(RuntimeFailure, match="permission"):
        await manager.run_now(SCOPE, service.id, request_key="not-authorized")
    assert secured.executed == []
    async with secured.catalog.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM operation")) == 0


async def test_proactive_preference_cannot_weaken_user_approval(automation: Automation) -> None:
    content = (
        await automation.kernel.catalog.registry.get_version(
            SCOPE,
            automation.configuration.target.capability_id,
            1,
        )
    ).content
    decision = ApprovalResolver().resolve(
        content,
        RiskEvaluator().evaluate(content),
        UserSecurityPolicy(preference=ApprovalPreference.ALWAYS),
        proactive_preference=ApprovalPreference.MINIMUM_REQUIRED,
    )
    assert "user_always" in decision.reasons


async def test_manual_run_still_checks_conditions(automation: Automation) -> None:
    config = automation.configuration.model_copy(
        update={
            "trigger": ManualTrigger(),
            "conditions": (Condition(path=("payload", "confirm"), operator="eq", value=True),),
        }
    )
    service = await automation.manager.create(SCOPE, config, enabled=True)
    with pytest.raises(RuntimeFailure, match="matching conditions"):
        await automation.manager.run_now(SCOPE, service.id, request_key="conditions")
    assert (
        await automation.manager.run_now(
            SCOPE,
            service.id,
            request_key="conditions",
            payload={"confirm": True},
        )
        is not None
    )


async def test_config_migration_and_revision_guards(automation: Automation) -> None:
    service = await automation.manager.create(SCOPE, automation.configuration)
    engine = automation.kernel.catalog.sessions.kw["bind"]
    async with engine.begin() as connection:
        await connection.exec_driver_sql("DELETE FROM schema_migration WHERE version=11")
        await connection.exec_driver_sql("DROP TRIGGER proactive_revision_guard")
        await connection.exec_driver_sql("DROP INDEX idx_execution_proactive")
    await upgrade_database(engine)
    await upgrade_database(engine)
    assert await automation.repository.get(SCOPE, service.id) == service
    with pytest.raises(IntegrityError, match="new revision"):
        async with automation.kernel.catalog.sessions() as session:
            await session.execute(text("UPDATE proactive_service SET enabled=1"))
            await session.commit()


async def test_hosted_automation_uses_existing_runtime_and_stops_transport(runtime: Runtime) -> None:
    worker = AutomationWorker(lambda: runtime)
    stopping = asyncio.Event()
    await worker.start(stopping)
    await runtime.events.publish(event())
    await runtime.events.join()
    stopping.set()
    await worker.stop(stopping)
    with pytest.raises(RuntimeFailure, match="accepting"):
        await runtime.events.publish(event("after-stop"))
