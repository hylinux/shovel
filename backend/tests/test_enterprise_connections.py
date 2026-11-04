import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from shovel.capability.domain import CapabilityKind, CapabilityScope
from shovel.cli.app import app
from shovel.db.enterprise_repository import SQLiteConnectionRepository
from shovel.db.migrations import upgrade_database
from shovel.db.model.retrieval import SchemaMigration
from shovel.enterprise.catalog import InstalledProviderCatalog
from shovel.enterprise.domain import Cloud, ConnectionDraft, ProviderSetting
from shovel.enterprise.service import ConnectionService
from shovel.identity.domain import AuthContext
from shovel.platform.domain import PlatformCommand
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.service import Runtime
from shovel.security.domain import SecurityDenied
from test_capability_registry import SCOPE, Catalog, skill
from test_capability_registry import catalog as catalog
from test_enterprise_connection_contracts import manifest
from test_enterprise_identity_contracts import TENANT
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime


def draft(**changes: object) -> ConnectionDraft:
    return ConnectionDraft.model_validate(
        {
            "display_name": "Enterprise draft",
            "provider_id": "provider",
            "connection_type": "query",
            "environment": "production",
            "cloud": Cloud.AZURE_GLOBAL,
            "endpoint": "https://cluster.example",
            "tenant_binding": TENANT,
            "resource_scope": ("database",),
            "identity_profile_ref": "pending-identity",
            "protection_profile_ref": "pending-protection",
            **changes,
        }
    )


@pytest.fixture
def connections(catalog: Catalog) -> ConnectionService:
    return ConnectionService(
        SQLiteConnectionRepository(catalog.sessions, clock=lambda: 10),
        InstalledProviderCatalog((manifest(),), runtime_version="0.1.2"),
    )


async def test_drafts_roundtrip_restart_update_disable_and_retain_server_owned_metadata(
    connections: ConnectionService,
    catalog: Catalog,
) -> None:
    created = await connections.create(SCOPE, draft())
    identifier = created.profile.id
    assert created.profile.revision == 1 and not created.profile.enabled and created.profile.status == "draft"
    assert created.manifest_digest == manifest().content_hash
    restarted = ConnectionService(SQLiteConnectionRepository(catalog.sessions), connections.catalog)
    assert await restarted.get(SCOPE, identifier) == created
    verdict = await restarted.validate(SCOPE, identifier, expected_revision=1)
    assert verdict.configuration_valid and not verdict.executable
    assert set(verdict.pending_requirements) == {"identity", "protection", "transport"}
    updated = await connections.update(SCOPE, identifier, draft(display_name="Edited"), expected_revision=1)
    assert updated.profile.revision == 2 and updated.profile.created_at == created.profile.created_at
    assert updated.profile.status == "draft" and not updated.profile.enabled
    disabled = await connections.disable(SCOPE, identifier, expected_revision=2)
    assert disabled.profile.revision == 3 and disabled.profile.status == "disabled"
    assert not disabled.profile.enabled and (await connections.list(SCOPE)) == [disabled]


@pytest.mark.parametrize(
    "other",
    [
        CapabilityScope(workspace_id="other", user_id=SCOPE.user_id),
        CapabilityScope(workspace_id=SCOPE.workspace_id, user_id="other"),
    ],
)
async def test_reads_and_all_mutations_are_scope_isolated(connections: ConnectionService, other) -> None:
    record = await connections.create(SCOPE, draft())
    assert await connections.list(other) == []
    for operation in (
        connections.get(other, record.profile.id),
        connections.update(other, record.profile.id, draft(), expected_revision=1),
        connections.disable(other, record.profile.id, expected_revision=1),
        connections.validate(other, record.profile.id, expected_revision=1),
    ):
        with pytest.raises(NotFound):
            await operation


async def test_concurrent_revision_edits_have_exactly_one_winner(connections: ConnectionService) -> None:
    record = await connections.create(SCOPE, draft())
    results = await asyncio.gather(
        connections.update(SCOPE, record.profile.id, draft(display_name="First"), expected_revision=1),
        connections.disable(SCOPE, record.profile.id, expected_revision=1),
        return_exceptions=True,
    )
    conflicts = [result for result in results if isinstance(result, RuntimeFailure)]
    assert len(conflicts) == 1 and conflicts[0].status == 409
    assert (await connections.get(SCOPE, record.profile.id)).profile.revision == 2
    with pytest.raises(RuntimeFailure) as caught:
        await connections.validate(SCOPE, record.profile.id, expected_revision=1)
    assert caught.value.status == 409


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_id": "uninstalled"},
        {"endpoint": "https://unreviewed.example"},
        {"cloud": Cloud.AZURE_CHINA},
        {"connection_type": "unknown"},
        {"environment": "unreviewed"},
        {"provider_settings": (ProviderSetting(name="unreviewed", value="value"),)},
    ],
)
async def test_manifest_mismatches_do_not_commit_drafts(connections: ConnectionService, changes) -> None:
    with pytest.raises(RuntimeFailure):
        await connections.create(SCOPE, draft(**changes))
    assert await connections.list(SCOPE) == []


async def test_manifest_replacement_requires_a_new_reviewed_revision(
    connections: ConnectionService,
    catalog: Catalog,
) -> None:
    record = await connections.create(SCOPE, draft())
    replacement = manifest().model_copy(update={"provider_version": "2.0"})
    replaced = ConnectionService(
        SQLiteConnectionRepository(catalog.sessions),
        InstalledProviderCatalog((replacement,), runtime_version="0.1.2"),
    )
    with pytest.raises(RuntimeFailure, match="manifest has changed"):
        await replaced.validate(SCOPE, record.profile.id, expected_revision=1)
    updated = await replaced.update(SCOPE, record.profile.id, draft(), expected_revision=1)
    assert updated.manifest_digest == replacement.content_hash and updated.manifest_version == "2.0"
    assert not (await replaced.validate(SCOPE, record.profile.id, expected_revision=2)).executable
    removed = ConnectionService(
        SQLiteConnectionRepository(catalog.sessions),
        InstalledProviderCatalog(runtime_version="0.1.2"),
    )
    with pytest.raises(RuntimeFailure, match="trusted installed catalog"):
        await removed.validate(SCOPE, record.profile.id, expected_revision=2)
    assert (await removed.disable(SCOPE, record.profile.id, expected_revision=2)).profile.status == "disabled"


@pytest.mark.parametrize(
    "manifests",
    [
        (manifest(), manifest()),
        (manifest().model_copy(update={"minimum_runtime_version": "9.0.0"}),),
        (manifest().model_copy(update={"minimum_runtime_version": "unknown"}),),
    ],
)
def test_installed_catalog_rejects_duplicate_or_incompatible_providers(manifests) -> None:
    with pytest.raises(RuntimeFailure):
        InstalledProviderCatalog(manifests, runtime_version="0.1.2")


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": True},
        {"status": "validated"},
        {"revision": 1},
        {"id": "user-id"},
        {"created_at": 1},
        {"manifest": manifest().model_dump(mode="json")},
        {"provider_settings": [{"name": "cookie", "value": "CONFIG-SENTINEL"}]},
    ],
)
def test_draft_does_not_accept_lifecycle_manifest_or_credentials(changes) -> None:
    with pytest.raises(ValidationError):
        draft(**changes)


async def test_upgrade_from_previous_schema_preserves_receipts_hashes_and_installs_guards(
    catalog: Catalog,
    connections: ConnectionService,
) -> None:
    created = await catalog.service.create_draft(SCOPE, CapabilityKind.SKILL, "Existing", skill())
    published = await catalog.service.publish(SCOPE, created.capability_id, 1, expected_revision=1)
    async with catalog.sessions() as session:
        engine = session.bind
    assert isinstance(engine, AsyncEngine)
    async with engine.begin() as connection:
        await connection.exec_driver_sql("DROP TABLE enterprise_connection_profile")
        await connection.exec_driver_sql("DELETE FROM schema_migration WHERE version=15")
        await connection.exec_driver_sql(
            """INSERT INTO operation(
            id,kind,state,payload,result,queued_at,attempts,request_key,request_fingerprint)
            VALUES ('legacy-es3','cleanup','succeeded','{}','{"kept":true}',
            1,1,'legacy-key','legacy-fingerprint')"""
        )
    await upgrade_database(engine)
    await upgrade_database(engine)
    assert await catalog.registry.get_version(SCOPE, created.capability_id, 1) == published
    async with catalog.sessions() as session:
        assert list(
            await session.scalars(select(SchemaMigration.version).order_by(SchemaMigration.version))
        ) == list(range(1, 21))
        row = (
            await session.execute(
                text("SELECT result,request_key,request_fingerprint FROM operation WHERE id='legacy-es3'")
            )
        ).one()
        assert tuple(row) == ('{"kept":true}', "legacy-key", "legacy-fingerprint")
    record = await connections.create(SCOPE, draft())
    for assignment in (
        "enabled=1,revision=revision+1",
        "status='validated',revision=revision+1",
        "revision=revision+2",
        "workspace_id='other',revision=revision+1",
    ):
        async with catalog.sessions() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(f"UPDATE enterprise_connection_profile SET {assignment} WHERE id=:id"),
                    {"id": record.profile.id},
                )
            await session.rollback()
    assert (await connections.get(SCOPE, record.profile.id)).profile.revision == 1


async def test_platform_http_commands_remain_disabled_and_do_not_create_auth_or_network(
    runtime: Runtime,
    api: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (await api.get("/api/v1/platform/enterprise/connections/options")).json() == {"items": []}
    runtime.platform.connections = ConnectionService(
        SQLiteConnectionRepository(runtime.sessions),
        InstalledProviderCatalog((manifest(),), runtime_version="0.1.2"),
    )
    network = AsyncMock(side_effect=AssertionError("Connection metadata must not leave the process"))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network)
    monkeypatch.setattr(AuthContext, "__init__", lambda *args, **kwargs: pytest.fail("No authentication"))
    command = {"action": "enterprise.connection.create", "input": draft().model_dump(mode="json")}
    response = await api.post("/api/v1/platform/commands", json=command)
    assert response.status_code == 200
    record = response.json()
    identifier = record["profile"]["id"]
    assert not record["profile"]["enabled"]
    assert (await api.get(f"/api/v1/platform/enterprise/connections/{identifier}")).json() == record
    assert (await api.get("/api/v1/platform/enterprise/connections")).json() == {"items": [record]}
    validation = await api.post(
        "/api/v1/platform/commands",
        json={
            "action": "enterprise.connection.validate",
            "identifier": identifier,
            "expected_revision": 1,
        },
    )
    assert validation.status_code == 200 and not validation.json()["executable"]
    rejected = await api.post(
        "/api/v1/platform/commands",
        json={
            **command,
            "input": {
                **command["input"],
                "provider_settings": [{"name": "cookie", "value": "CONFIG-SENTINEL"}],
            },
        },
    )
    assert rejected.status_code == 422 and "CONFIG-SENTINEL" not in rejected.text
    rejected_key = await api.post(
        "/api/v1/platform/commands",
        json={**command, "input": {**command["input"], "CONFIG-SENTINEL": "untrusted"}},
    )
    assert rejected_key.status_code == 422 and "CONFIG-SENTINEL" not in rejected_key.text
    assert (
        await api.post(
            "/api/v1/platform/commands",
            json={
                "action": "enterprise.connection.enable",
                "identifier": identifier,
            },
        )
    ).status_code == 422
    with pytest.raises(SecurityDenied):
        await runtime.platform.execute(PlatformCommand(action="enterprise.connection.list"), human=False)
    network.assert_not_called()


def test_connection_cli_uses_platform_boundary_and_requires_revisions(monkeypatch, tmp_path) -> None:
    commands = []
    monkeypatch.setattr("shovel.cli.commands.platform._run", commands.append)
    path = tmp_path / "draft.json"
    path.write_text(draft().model_dump_json(), encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(app, ["connection", "create", str(path)]).exit_code == 0
    assert commands[0].action == "enterprise.connection.create"
    assert commands[0].input == draft().model_dump(mode="json")
    assert runner.invoke(app, ["connection", "update", "connection", str(path)]).exit_code != 0
    assert runner.invoke(app, ["connection", "disable", "connection", "--revision", "2"]).exit_code == 0
    assert commands[-1].expected_revision == 2
    path.write_text(json.dumps({**draft().model_dump(mode="json"), "enabled": True}), encoding="utf-8")
    assert runner.invoke(app, ["connection", "create", str(path)]).exit_code != 0
