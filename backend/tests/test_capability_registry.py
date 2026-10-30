from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import (
    AgentImplementation,
    Availability,
    CapabilityContent,
    CapabilityKind,
    CapabilityReference,
    CapabilityScope,
    CapabilityState,
    CapabilityVersion,
    ConnectionRequirement,
    DependencyLock,
    FunctionContract,
    FunctionImplementation,
    LockedDependency,
    Permission,
    RiskProfile,
    SideEffectProfile,
    SkillImplementation,
    WorkflowEdge,
    WorkflowImplementation,
    WorkflowNode,
)
from shovel.capability.registry import CapabilityRegistry
from shovel.capability.service import CapabilityService
from shovel.db.capability_repository import SQLiteCapabilityRepository
from shovel.db.database import create_async_database_engine, create_async_session_factory
from shovel.db.migrations import upgrade_database
from shovel.db.model.capability import CapabilityDefinition as DefinitionRow
from shovel.db.model.capability import CapabilityVersion as VersionRow
from shovel.runtime.errors import NotFound, RuntimeFailure

SCOPE = CapabilityScope(workspace_id="workspace", user_id="owner")


class ExplicitPublisher:
    def __init__(self, scope: CapabilityScope = SCOPE) -> None:
        self.scope = scope
        self.calls: list[tuple[str, str, int | None, str]] = []

    async def authorize(
        self,
        scope: CapabilityScope,
        action: Literal["publish", "availability"],
        capability_id: str,
        version: int | None,
        content_hash: str,
    ) -> None:
        assert scope == self.scope
        self.calls.append((action, capability_id, version, content_hash))


@dataclass
class Catalog:
    repository: SQLiteCapabilityRepository
    registry: CapabilityRegistry
    service: CapabilityService
    publisher: ExplicitPublisher
    sessions: async_sessionmaker[AsyncSession]


@pytest.fixture
async def catalog(tmp_path: Path) -> AsyncIterator[Catalog]:
    context = create_async_database_engine(f"sqlite+aiosqlite:///{(tmp_path / 'catalog.db').as_posix()}")
    engine = await anext(context)
    try:
        await upgrade_database(engine)
        sessions = create_async_session_factory(engine)
        repository = SQLiteCapabilityRepository(sessions)
        registry = CapabilityRegistry(
            repository, functions=(FunctionContract(implementation_id="demo.read"),)
        )
        publisher = ExplicitPublisher()
        yield Catalog(
            repository, registry, CapabilityService(registry, authorizer=publisher), publisher, sessions
        )
    finally:
        await context.aclose()


def skill(instructions: str = "Summarize the supplied text.") -> CapabilityContent:
    return CapabilityContent(implementation=SkillImplementation(instructions=instructions))


async def published(catalog: Catalog, content: CapabilityContent | None = None) -> CapabilityVersion:
    draft = await catalog.service.create_draft(SCOPE, CapabilityKind.SKILL, "Summary", content or skill())
    return await catalog.service.publish(
        SCOPE, draft.capability_id, draft.number, expected_revision=draft.revision
    )


def locked(version: CapabilityVersion) -> LockedDependency:
    assert version.content_hash is not None
    return LockedDependency(
        capability_id=version.capability_id,
        version=version.number,
        content_hash=version.content_hash,
    )


@pytest.mark.parametrize("kind", list(CapabilityKind))
async def test_create_validate_publish_and_resolve_all_kinds(catalog: Catalog, kind: CapabilityKind) -> None:
    implementations = {
        CapabilityKind.FUNCTION: FunctionImplementation(implementation_id="demo.read"),
        CapabilityKind.SKILL: SkillImplementation(instructions="Summarize text."),
        CapabilityKind.AGENT: AgentImplementation(instructions="Answer using the supplied context."),
        CapabilityKind.WORKFLOW: WorkflowImplementation(
            nodes=(WorkflowNode(id="start", kind="input"), WorkflowNode(id="end", kind="output")),
            edges=(WorkflowEdge(source="start", target="end"),),
        ),
    }
    content = CapabilityContent(implementation=implementations[kind])
    draft = await catalog.service.create_draft(SCOPE, kind, kind.value, content)
    definition = await catalog.registry.get(SCOPE, draft.capability_id)
    assert definition.kind == kind and definition.scope == SCOPE
    assert draft.state == CapabilityState.DRAFT and draft.content == content
    assert (await catalog.service.validate_draft(SCOPE, definition.id, 1)).valid
    with pytest.raises(RuntimeFailure, match="Only published"):
        await catalog.registry.resolve_exact(SCOPE, definition.id, 1)
    version = await catalog.service.publish(SCOPE, definition.id, 1, expected_revision=1)
    assert version.state == CapabilityState.PUBLISHED and version.published_by == SCOPE.user_id
    assert version.content_hash == content.content_hash
    assert catalog.publisher.calls == [("publish", definition.id, 1, content.content_hash)]
    assert await catalog.registry.resolve_exact(SCOPE, definition.id, 1) == version


async def test_update_draft_conflicts_and_published_edit_requires_new_version(catalog: Catalog) -> None:
    draft = await catalog.service.create_draft(SCOPE, CapabilityKind.SKILL, "Summary", skill())
    updated = await catalog.service.update_draft(
        SCOPE,
        draft.capability_id,
        1,
        skill("Use a short summary."),
        expected_revision=1,
    )
    assert updated.revision == 2 and updated.content != draft.content
    with pytest.raises(RuntimeFailure, match="has changed"):
        await catalog.service.update_draft(SCOPE, draft.capability_id, 1, skill(), expected_revision=1)
    version = await catalog.service.publish(SCOPE, draft.capability_id, 1, expected_revision=2)
    with pytest.raises(RuntimeFailure, match="immutable"):
        await catalog.service.update_draft(SCOPE, draft.capability_id, 1, skill(), expected_revision=3)
    second = await catalog.service.create_draft_from_version(SCOPE, draft.capability_id, 1)
    assert second.number == 2 and second.state == CapabilityState.DRAFT
    assert second.based_on_version_id == version.id and second.content == version.content
    await catalog.service.update_draft(SCOPE, draft.capability_id, 2, skill(), expected_revision=1)
    assert await catalog.registry.resolve_exact(SCOPE, draft.capability_id, 1) == version
    async with catalog.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(DefinitionRow)) == 1
        assert await session.scalar(select(func.count()).select_from(VersionRow)) == 2


async def test_publish_is_fail_closed_and_binds_revision_and_content(catalog: Catalog) -> None:
    draft = await catalog.service.create_draft(SCOPE, CapabilityKind.SKILL, "Summary", skill())
    with pytest.raises(RuntimeFailure, match="authorization is not configured"):
        await CapabilityService(catalog.registry).publish(SCOPE, draft.capability_id, 1, expected_revision=1)
    with pytest.raises(RuntimeFailure, match="has changed"):
        await catalog.service.publish(SCOPE, draft.capability_id, 1, expected_revision=2)

    class ConcurrentEditor(ExplicitPublisher):
        async def authorize(
            self,
            scope: CapabilityScope,
            action: Literal["publish", "availability"],
            capability_id: str,
            version: int | None,
            content_hash: str,
        ) -> None:
            await catalog.service.update_draft(
                scope, capability_id, 1, skill("Changed."), expected_revision=1
            )

    service = CapabilityService(catalog.registry, authorizer=ConcurrentEditor())
    with pytest.raises(RuntimeFailure, match="has changed"):
        await service.publish(SCOPE, draft.capability_id, 1, expected_revision=1)
    assert (await catalog.registry.get_version(SCOPE, draft.capability_id, 1)).state == CapabilityState.DRAFT


@pytest.mark.parametrize("definition_wide", [False, True])
@pytest.mark.parametrize("availability", [Availability.DISABLED, Availability.ARCHIVED])
async def test_disabled_or_archived_capability_cannot_resolve(
    catalog: Catalog,
    definition_wide: bool,
    availability: Availability,
) -> None:
    version = await published(catalog)
    await catalog.service.set_availability(
        SCOPE,
        version.capability_id,
        availability,
        number=None if definition_wide else 1,
    )
    with pytest.raises(RuntimeFailure, match="unavailable"):
        await catalog.registry.resolve_exact(SCOPE, version.capability_id, 1)
    saved = await catalog.registry.get_version(SCOPE, version.capability_id, 1)
    assert saved.content == version.content and saved.content_hash == version.content_hash
    await catalog.service.set_availability(
        SCOPE,
        version.capability_id,
        Availability.ACTIVE,
        number=None if definition_wide else 1,
    )
    assert await catalog.registry.resolve_exact(SCOPE, version.capability_id, 1) == version


async def test_deprecated_version_remains_explicitly_resolvable_without_mutation(catalog: Catalog) -> None:
    version = await published(catalog)
    await catalog.service.set_availability(SCOPE, version.capability_id, Availability.DEPRECATED, number=1)
    resolved = await catalog.registry.resolve_exact(SCOPE, version.capability_id, 1)
    assert resolved.state == CapabilityState.DEPRECATED and resolved.content_hash == version.content_hash


async def test_scope_and_literal_search_isolation(catalog: Catalog) -> None:
    first = await catalog.service.create_draft(SCOPE, CapabilityKind.SKILL, "100% summary", skill())
    await catalog.service.create_draft(SCOPE, CapabilityKind.SKILL, "Other", skill())
    other = CapabilityScope(workspace_id=SCOPE.workspace_id, user_id="other")
    await catalog.service.create_draft(other, CapabilityKind.SKILL, "100% summary", skill())
    assert len(await catalog.registry.list(SCOPE)) == 2
    assert [item.id for item in await catalog.registry.search(SCOPE, "%")] == [first.capability_id]
    with pytest.raises(NotFound):
        await catalog.registry.get_version(other, first.capability_id, 1)
    with pytest.raises(NotFound):
        await catalog.service.update_draft(other, first.capability_id, 1, skill(), expected_revision=1)
    with pytest.raises(RuntimeFailure, match="pagination"):
        await catalog.registry.list(SCOPE, limit=0)


async def test_all_version_metadata_round_trips_and_dependency_revalidation(catalog: Catalog) -> None:
    target = await published(catalog)
    permission = Permission(action="knowledge.read", resource="resource-a")
    content = CapabilityContent(
        implementation=SkillImplementation(instructions="Use only the declared capability."),
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        output_schema={"type": "string"},
        required_permissions=(permission,),
        permission_boundary=(permission,),
        risk_profile=RiskProfile(level="high", reasons=("Sensitive input.",)),
        side_effect_profile=SideEffectProfile(kind="local_write", description="Draft only."),
        approval_requirement="always",
        allowed_capabilities=(CapabilityReference(capability_id=target.capability_id, version=1),),
        dependency_lock=DependencyLock(dependencies=(locked(target),)),
        connection_requirements=(ConnectionRequirement(name="documents", connection_ref="local-documents"),),
        provenance={"source": "explicit-user-draft", "revision": 1},
    )
    version = await published(catalog, content)
    assert (await catalog.registry.resolve_exact(SCOPE, version.capability_id, 1)).content == content
    await catalog.service.set_availability(SCOPE, target.capability_id, Availability.DISABLED)
    with pytest.raises(RuntimeFailure, match="Dependency is unavailable"):
        await catalog.registry.resolve_exact(SCOPE, version.capability_id, 1)


async def test_dependency_lock_rejects_drafts_wrong_hash_and_incomplete_transitive_lock(
    catalog: Catalog,
) -> None:
    draft = await catalog.service.create_draft(SCOPE, CapabilityKind.SKILL, "Draft dependency", skill())
    bad = skill().model_copy(
        update={
            "dependency_lock": DependencyLock(
                dependencies=(
                    LockedDependency(capability_id=draft.capability_id, version=1, content_hash="0" * 64),
                )
            ),
        }
    )
    dependent = await catalog.service.create_draft(SCOPE, CapabilityKind.SKILL, "Dependent", bad)
    result = await catalog.service.validate_draft(SCOPE, dependent.capability_id, 1)
    assert not result.valid and "Dependency must be published." in result.errors
    with pytest.raises(RuntimeFailure, match="Invalid capability draft"):
        await catalog.service.publish(SCOPE, dependent.capability_id, 1, expected_revision=1)
    leaf = await published(catalog)
    parent = await published(
        catalog,
        skill().model_copy(update={"dependency_lock": DependencyLock(dependencies=(locked(leaf),))}),
    )
    incomplete = await catalog.service.create_draft(
        SCOPE,
        CapabilityKind.SKILL,
        "Incomplete",
        skill().model_copy(update={"dependency_lock": DependencyLock(dependencies=(locked(parent),))}),
    )
    result = await catalog.service.validate_draft(SCOPE, incomplete.capability_id, 1)
    assert "Dependency lock must include the complete transitive lock." in result.errors


async def test_dependency_availability_is_rechecked_in_publication_transaction(catalog: Catalog) -> None:
    target = await published(catalog)
    draft = await catalog.service.create_draft(
        SCOPE,
        CapabilityKind.SKILL,
        "Dependent",
        skill().model_copy(update={"dependency_lock": DependencyLock(dependencies=(locked(target),))}),
    )

    class ConcurrentRevocation(ExplicitPublisher):
        async def authorize(
            self,
            scope: CapabilityScope,
            action: Literal["publish", "availability"],
            capability_id: str,
            version: int | None,
            content_hash: str,
        ) -> None:
            await catalog.repository.set_availability(scope, target.capability_id, Availability.DISABLED)

    service = CapabilityService(catalog.registry, authorizer=ConcurrentRevocation())
    with pytest.raises(RuntimeFailure, match="Dependency is unavailable"):
        await service.publish(SCOPE, draft.capability_id, 1, expected_revision=1)


async def test_concurrent_forks_allocate_unique_versions(catalog: Catalog) -> None:
    version = await published(catalog)
    forks = await asyncio.gather(
        *(catalog.service.create_draft_from_version(SCOPE, version.capability_id, 1) for _ in range(3))
    )
    assert sorted(item.number for item in forks) == [2, 3, 4]
    async with catalog.sessions() as session:
        session.add(
            VersionRow(
                capability_id=version.capability_id,
                number=2,
                content=skill().model_dump(mode="json"),
            )
        )
        with pytest.raises(IntegrityError, match="UNIQUE"):
            await session.commit()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE capability_version SET content = '{}' WHERE id = :id",
        "UPDATE capability_version SET content_hash = :hash WHERE id = :id",
        "UPDATE capability_version SET published_at = NULL, published_by = NULL, "
        "content_hash = NULL WHERE id = :id",
        "DELETE FROM capability_version WHERE id = :id",
        "UPDATE capability_definition SET kind = 'agent' WHERE id = :cap",
        "UPDATE capability_definition SET user_id = 'other' WHERE id = :cap",
        "DELETE FROM capability_version_availability WHERE version_id = :id",
        "INSERT OR REPLACE INTO capability_version "
        "SELECT id, capability_id, number, revision, '{}', content_hash, created_at, "
        "published_at, published_by, based_on_version_id FROM capability_version WHERE id = :id",
        "INSERT OR REPLACE INTO capability_definition "
        "SELECT id, workspace_id, 'other', kind, name, description, availability, created_at "
        "FROM capability_definition WHERE id = :cap",
    ],
)
async def test_database_guards_published_content_and_identity(catalog: Catalog, sql: str) -> None:
    version = await published(catalog)
    async with catalog.sessions() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(sql), {"id": version.id, "cap": version.capability_id, "hash": "0" * 64}
            )
    assert await catalog.registry.resolve_exact(SCOPE, version.capability_id, 1) == version


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM capability_dependency WHERE owner_version_id = :id",
        "UPDATE capability_dependency SET target_version_id = :target WHERE owner_version_id = :id",
        "INSERT INTO capability_dependency(owner_version_id, target_version_id) VALUES (:id, :target)",
    ],
)
async def test_published_dependency_edges_are_immutable(catalog: Catalog, sql: str) -> None:
    target = await published(catalog)
    version = await published(
        catalog,
        skill().model_copy(update={"dependency_lock": DependencyLock(dependencies=(locked(target),))}),
    )
    async with catalog.sessions() as session:
        with pytest.raises(IntegrityError, match="dependency lock is immutable"):
            await session.execute(text(sql), {"id": version.id, "target": target.id})


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "not-a-json-schema-type"},
        {"$ref": "https://example.invalid/schema"},
        {"$dynamicRef": "file:///private/schema"},
        {"$schema": "https://example.invalid/meta"},
    ],
)
async def test_invalid_or_remote_schema_cannot_publish(catalog: Catalog, schema: dict[str, str]) -> None:
    content = skill().model_copy(update={"input_schema": schema})
    draft = await catalog.service.create_draft(SCOPE, CapabilityKind.SKILL, "Invalid schema", content)
    assert not (await catalog.service.validate_draft(SCOPE, draft.capability_id, 1)).valid
    with pytest.raises(RuntimeFailure, match="Invalid capability draft"):
        await catalog.service.publish(SCOPE, draft.capability_id, 1, expected_revision=1)
    assert not catalog.publisher.calls


async def test_user_implementation_ids_never_import_or_execute_code(
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Capability metadata attempted to import or execute user code.")

    monkeypatch.setattr(importlib.util, "spec_from_file_location", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    content = CapabilityContent(implementation=FunctionImplementation(implementation_id="os.system"))
    draft = await catalog.service.create_draft(SCOPE, CapabilityKind.FUNCTION, "Untrusted", content)
    with pytest.raises(RuntimeFailure, match="not registered"):
        await catalog.service.publish(SCOPE, draft.capability_id, 1, expected_revision=1)
    for kind in ("python", "javascript", "shell", "eval", "exec"):
        with pytest.raises(ValidationError):
            CapabilityContent.model_validate({"implementation": {"kind": kind, "code": "untrusted"}})
    with pytest.raises(ValidationError):
        CapabilityContent.model_validate(
            {
                "implementation": {"kind": "function", "implementation_id": "demo.read", "module": "os"},
            }
        )
    for kind in ("script", "loop", "parallel", "join", "schedule", "event_trigger", "cron"):
        with pytest.raises(ValidationError):
            WorkflowNode.model_validate({"id": "bad", "kind": kind})


async def test_function_metadata_cannot_understate_trusted_contract(catalog: Catalog) -> None:
    registry = CapabilityRegistry(
        catalog.repository,
        functions=(FunctionContract(implementation_id="sensitive", risk_profile=RiskProfile(level="high")),),
    )
    service = CapabilityService(registry, authorizer=catalog.publisher)
    draft = await service.create_draft(
        SCOPE,
        CapabilityKind.FUNCTION,
        "Sensitive",
        CapabilityContent(implementation=FunctionImplementation(implementation_id="sensitive")),
    )
    with pytest.raises(RuntimeFailure, match="risk_profile does not match"):
        await service.publish(SCOPE, draft.capability_id, 1, expected_revision=1)


async def test_storage_revalidates_mutable_nested_metadata_and_enforces_size(catalog: Catalog) -> None:
    content = skill()
    content.provenance["oversized"] = "x" * 262144
    with pytest.raises(ValidationError, match="256 KiB"):
        await catalog.service.create_draft(SCOPE, CapabilityKind.SKILL, "Too large", content)
    assert not await catalog.registry.list(SCOPE)


async def test_workflow_cycle_is_rejected_without_executing_a_workflow(catalog: Catalog) -> None:
    content = CapabilityContent(
        implementation=WorkflowImplementation(
            nodes=(WorkflowNode(id="start", kind="input"), WorkflowNode(id="end", kind="output")),
            edges=(WorkflowEdge(source="start", target="end"), WorkflowEdge(source="end", target="start")),
        )
    )
    draft = await catalog.service.create_draft(SCOPE, CapabilityKind.WORKFLOW, "Cycle", content)
    with pytest.raises(RuntimeFailure, match="cycles are not allowed"):
        await catalog.service.publish(SCOPE, draft.capability_id, 1, expected_revision=1)
