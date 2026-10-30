from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from shovel.automation.domain import EventTrigger, NormalizedEvent, ProactiveConfiguration, TargetCapability
from shovel.automation.schedules import next_occurrence
from shovel.capability.domain import (
    CapabilityContent,
    CapabilityKind,
    CapabilityReference,
    DependencyLock,
    LockedDependency,
    Permission,
    SkillImplementation,
)
from shovel.db.database import create_async_database_engine
from shovel.db.migrations import upgrade_database
from shovel.platform.domain import PlatformCommand
from shovel.runtime.service import Runtime
from shovel.security.domain import SecurityDenied, UserSecurityPolicy
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime
from test_platform_entries import confirmed, create, graph, publish


async def test_editor_updates_atomic_revision_and_forks_immutable_version(
    runtime: Runtime,
    api: httpx.AsyncClient,
) -> None:
    draft = await create(runtime)
    command = PlatformCommand(
        action="capability.update",
        identifier=draft["capability_id"],
        version=1,
        expected_revision=1,
        name="Edited skill",
        description="Editor description",
        content=CapabilityContent(implementation=SkillImplementation(instructions="Edited.")),
    )
    saved = await api.post("/api/v1/platform/commands", json=command.model_dump(mode="json"))
    assert saved.status_code == 200 and saved.json()["revision"] == 2
    assert (
        await api.post("/api/v1/platform/commands", json=command.model_dump(mode="json"))
    ).status_code == 409
    definition = (await api.get(f"/api/v1/platform/capabilities/{draft['capability_id']}")).json()
    assert definition["name"] == "Edited skill" and definition["description"] == "Editor description"
    published = await confirmed(
        runtime,
        PlatformCommand(
            action="capability.publish",
            identifier=draft["capability_id"],
            version=1,
            expected_revision=2,
        ),
    )
    command = command.model_copy(update={"expected_revision": 2, "name": "Must not change"})
    assert (
        await api.post("/api/v1/platform/commands", json=command.model_dump(mode="json"))
    ).status_code == 409
    fork = await runtime.platform.execute(
        PlatformCommand(
            action="capability.fork",
            identifier=draft["capability_id"],
            version=1,
        ),
        human=True,
    )
    assert fork["number"] == 2 and fork["published_at"] is None
    assert fork["content"] == published["content"]
    versions = (await api.get(f"/api/v1/platform/capabilities/{draft['capability_id']}/versions")).json()
    assert [row["number"] for row in versions["items"]] == [2, 1]


async def test_picker_and_forged_dependency_recheck_live_permissions(
    runtime: Runtime,
    api: httpx.AsyncClient,
) -> None:
    permission = Permission(action="read", resource="private")
    runtime.settings.platform.security = UserSecurityPolicy(
        user_grants=(permission,),
        platform_constraints=(permission,),
    )
    child = await publish(
        runtime,
        CapabilityContent(
            implementation=SkillImplementation(instructions="Restricted."),
            required_permissions=(permission,),
            permission_boundary=(permission,),
        ),
    )
    options = (await api.get("/api/v1/platform/options")).json()
    assert len(options["items"]) == 1 and options["memory_access"] == "disabled"
    content = CapabilityContent(
        implementation=SkillImplementation(instructions="Parent."),
        allowed_capabilities=(CapabilityReference(capability_id=child["capability_id"], version=1),),
        dependency_lock=DependencyLock(
            dependencies=(
                LockedDependency(
                    capability_id=child["capability_id"],
                    version=1,
                    content_hash=child["content_hash"],
                ),
            )
        ),
    )
    draft = await create(runtime, content)
    command = PlatformCommand(
        action="capability.publish",
        identifier=draft["capability_id"],
        version=1,
        expected_revision=1,
    )
    ticket = await runtime.platform.execute(command.model_copy(update={"prepare": True}), human=True)
    runtime.settings.platform.security = UserSecurityPolicy()
    assert not (await api.get("/api/v1/platform/options")).json()["items"]
    forged = PlatformCommand(
        action="capability.create", kind=CapabilityKind.SKILL, name="Forged", content=content
    )
    assert (
        await api.post("/api/v1/platform/commands", json=forged.model_dump(mode="json"))
    ).status_code == 403
    with pytest.raises(SecurityDenied):
        await runtime.platform.execute(
            command.model_copy(update={"confirmation_id": ticket["confirmation_id"]}), human=True
        )


async def test_global_pause_persists_preserves_services_allows_manual_and_skips_backlog(
    runtime: Runtime,
    api: httpx.AsyncClient,
) -> None:
    manager = runtime.platform.proactive
    now = [1000.0]
    manager.clock = manager.repository.clock = lambda: now[0]
    version = await publish(runtime, graph())
    config = ProactiveConfiguration(
        trigger=EventTrigger(event_type="test"),
        target=TargetCapability(capability_id=version["capability_id"], version=1),
    )
    draft = await runtime.platform.execute(
        PlatformCommand(
            action="proactive.create",
            name="Service",
            description="Visible",
            configuration=config,
        ),
        human=True,
    )
    disabled = await runtime.platform.execute(
        PlatformCommand(
            action="proactive.create",
            name="Keep disabled",
            configuration=config,
        ),
        human=True,
    )
    await confirmed(
        runtime, PlatformCommand(action="proactive.enable", identifier=draft["id"], expected_revision=1)
    )
    control = await runtime.platform.execute(
        PlatformCommand(action="automation.pause", expected_revision=1), human=True
    )
    from shovel.automation.manager import ProactiveServiceManager

    restarted = ProactiveServiceManager(manager.repository, manager.invocations)
    assert (await restarted.control(runtime.platform.scope)).paused
    event = NormalizedEvent(
        id="paused-event",
        scope=runtime.platform.scope,
        source="test",
        event_type="test",
        occurred_at=1000,
    )
    assert await manager.dispatch(runtime.platform.scope, draft["id"], event) is None
    manual = PlatformCommand(
        action="proactive.run", identifier=draft["id"], request_key="manual-while-paused"
    )
    queued = await runtime.platform.execute(manual, human=True)
    receipt = (
        await api.get(f"/api/v1/platform/requests/manual-while-paused?service_id={draft['id']}")
    ).json()
    assert receipt["execution"]["id"] == queued["id"]
    assert (await runtime.platform.execute(manual, human=True))["id"] == queued["id"]
    now[0] = 1001
    resume = PlatformCommand(action="automation.resume", expected_revision=control["revision"])
    assert (
        await api.post("/api/v1/platform/commands", json=resume.model_dump(mode="json"))
    ).status_code == 409
    await confirmed(runtime, resume)
    assert not (await manager.get(runtime.platform.scope, disabled["id"])).enabled
    assert await manager.dispatch(runtime.platform.scope, draft["id"], event) is None
    now[0] = 1002
    assert (
        await manager.dispatch(
            runtime.platform.scope,
            draft["id"],
            event.model_copy(
                update={
                    "id": "new-event",
                    "occurred_at": 1002,
                }
            ),
        )
        is not None
    )
    paused = await runtime.platform.execute(
        PlatformCommand(
            action="proactive.pause",
            identifier=draft["id"],
            expected_revision=2,
        ),
        human=True,
    )
    assert paused["enabled"] and paused["paused"]
    edited = await runtime.platform.execute(
        PlatformCommand(
            action="proactive.update",
            identifier=draft["id"],
            expected_revision=3,
            name="Edited",
            description="Changed",
            configuration=config,
        ),
        human=True,
    )
    assert edited["revision"] == 4 and not edited["enabled"]
    rows = (await api.get("/api/v1/platform/proactive")).json()["items"]
    assert next(row for row in rows if row["id"] == draft["id"])["name"] == "Edited"


async def test_global_resume_rechecks_permissions_and_approval_preview_is_server_bound(
    runtime: Runtime,
) -> None:
    permission = Permission(action="read", resource="private")
    runtime.settings.platform.security = UserSecurityPolicy(
        user_grants=(permission,),
        platform_constraints=(permission,),
    )
    version = await publish(
        runtime,
        graph().model_copy(
            update={
                "required_permissions": (permission,),
                "permission_boundary": (permission,),
            }
        ),
    )
    service = await runtime.platform.proactive.create(
        runtime.platform.scope,
        ProactiveConfiguration(
            trigger=EventTrigger(event_type="test"),
            target=TargetCapability(capability_id=version["capability_id"], version=1),
            permission_boundary=(permission,),
        ),
        enabled=True,
    )
    assert service.enabled
    await runtime.platform.execute(
        PlatformCommand(action="automation.pause", expected_revision=1), human=True
    )
    resume = PlatformCommand(action="automation.resume", expected_revision=2)
    ticket = await runtime.platform.execute(resume.model_copy(update={"prepare": True}), human=True)
    runtime.settings.platform.security = UserSecurityPolicy()
    with pytest.raises(SecurityDenied):
        await runtime.platform.execute(
            resume.model_copy(update={"confirmation_id": ticket["confirmation_id"]}), human=True
        )
    assert (await runtime.platform.proactive.control(runtime.platform.scope)).paused


async def test_schema14_upgrades_existing_service_without_rewriting_configuration(tmp_path: Path) -> None:
    context = create_async_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'old.db').as_posix()}")
    engine = await anext(context)
    try:
        await upgrade_database(engine)
        async with engine.begin() as connection:
            await connection.exec_driver_sql("DROP TRIGGER proactive_revision_guard")
            await connection.exec_driver_sql("DROP TABLE proactive_service")
            await connection.exec_driver_sql("""CREATE TABLE proactive_service (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL,
                enabled BOOLEAN NOT NULL, revision INTEGER NOT NULL, configuration TEXT NOT NULL,
                created_at FLOAT NOT NULL, updated_at FLOAT NOT NULL, schedule_cursor FLOAT NOT NULL)""")
            await connection.exec_driver_sql("""INSERT INTO proactive_service VALUES
                ('old','workspace','owner',1,5,'{"kept":true}',1,2,3)""")
            await connection.exec_driver_sql("DELETE FROM schema_migration WHERE version=14")
        await upgrade_database(engine)
        await upgrade_database(engine)
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT * FROM proactive_service"))).mappings().one()
            assert row["configuration"] == '{"kept":true}' and row["revision"] == 5
            assert row["name"] == "" and row["description"] == "" and row["paused"] == 0
    finally:
        await context.aclose()


def test_next_trigger_uses_same_timezone_and_dst_rules_as_scheduler() -> None:
    from shovel.automation.domain import ScheduleDefinition
    from test_automation import stamp

    daily = ScheduleDefinition(kind="daily", timezone="America/New_York", hour=2, minute=30)
    assert next_occurrence(daily, stamp("2025-03-08T08:00:00+00:00")) == stamp("2025-03-10T06:30:00+00:00")
    interval = ScheduleDefinition(kind="interval", anchor_at=100, interval_seconds=60)
    assert next_occurrence(interval, 160) == 220
