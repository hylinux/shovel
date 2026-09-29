"""ResourceService 的测试: 内存 SQLite + 内存凭据库。

这一层缝合了数据库、注册表与凭据库, 所以测试的重点是那些"缝错了才会
出问题"的地方: 创建后必须是 DRAFT、改配置必须回落 DRAFT、吊销必须销毁
凭据、以及密钥永远不进数据库。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shovel.db.base import Base
from shovel.db.model.config import Resource, ScanProfile
from shovel.domain.enums import ResourceState, ScanMode, Sensitivity
from shovel.exceptions.resource import (
    CredentialNotFoundError,
    DuplicateResourceNameError,
    InvalidConnectorConfigError,
    InvalidResourceStateTransitionError,
    ResourceNotFoundError,
    ResourceVerificationFailedError,
    UnknownConnectorError,
)
from shovel.services.credentials import CredentialRef, InMemoryCredentialStore
from shovel.services.resource_service import (
    DEFAULT_PROFILE_NAME,
    ResourceDraft,
    ResourceService,
)


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """一个建好表的内存库。

    必须显式打开 foreign_keys —— SQLite 默认是 OFF, 忘了开的话
    ON DELETE CASCADE 会静默失效, 而"删除资源会不会留下孤儿 profile"
    正是这里要验证的事情之一。
    """

    import shovel.db.model  # noqa: F401  导入即注册全部表

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn: Any, _record: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    await engine.dispose()


@pytest.fixture
def credentials() -> InMemoryCredentialStore:
    return InMemoryCredentialStore()


@pytest.fixture
def service(
    sessions: async_sessionmaker[AsyncSession],
    credentials: InMemoryCredentialStore,
) -> ResourceService:
    return ResourceService(sessions, credentials)


@pytest.fixture
def draft(tmp_path: Path) -> ResourceDraft:
    return ResourceDraft(
        name="notes",
        connector_kind="local_fs",
        config={"root_path": str(tmp_path)},
    )


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
async def test_create_starts_in_draft(service: ResourceService, draft: ResourceDraft) -> None:
    """创建只证明配置的形状对了, 不证明目录真的存在且可读。"""

    resource = await service.create(draft)

    assert resource.state is ResourceState.DRAFT
    assert resource.last_verified_at is None
    assert resource.id.startswith("res_")


async def test_create_applies_connector_defaults(
    service: ResourceService, draft: ResourceDraft
) -> None:
    """默认权威度/敏感度来自连接器 —— 只有它知道自己是哪种数据源。"""

    resource = await service.create(draft)

    assert resource.authority == pytest.approx(0.8)
    assert resource.sensitivity is Sensitivity.PERSONAL


async def test_explicit_values_beat_connector_defaults(
    service: ResourceService, tmp_path: Path
) -> None:
    resource = await service.create(
        ResourceDraft(
            name="public",
            connector_kind="local_fs",
            config={"root_path": str(tmp_path)},
            authority=0.2,
            sensitivity=Sensitivity.NORMAL,
            description="公开资料",
        )
    )

    assert resource.authority == pytest.approx(0.2)
    assert resource.sensitivity is Sensitivity.NORMAL
    assert resource.description == "公开资料"


async def test_create_makes_a_default_scan_profile(
    service: ResourceService,
    sessions: async_sessionmaker[AsyncSession],
    draft: ResourceDraft,
) -> None:
    """没有 profile 的资源永远不会被扫描, 用户却会看到它是 ACTIVE。"""

    resource = await service.create(draft)

    async with sessions() as session:
        result = await session.execute(
            select(ScanProfile).where(ScanProfile.resource_id == resource.id)
        )
        profiles = result.scalars().all()

    assert len(profiles) == 1
    assert profiles[0].name == DEFAULT_PROFILE_NAME
    # local_fs 支持增量, 默认就该是增量而不是全量
    assert profiles[0].scan_mode is ScanMode.INCREMENTAL


async def test_duplicate_name_is_rejected(
    service: ResourceService, draft: ResourceDraft
) -> None:
    await service.create(draft)

    with pytest.raises(DuplicateResourceNameError):
        await service.create(draft)


async def test_unknown_connector_is_rejected(service: ResourceService) -> None:
    with pytest.raises(UnknownConnectorError):
        await service.create(
            ResourceDraft(name="x", connector_kind="notion", config={})
        )


async def test_invalid_config_is_rejected(service: ResourceService) -> None:
    with pytest.raises(InvalidConnectorConfigError) as excinfo:
        await service.create(
            ResourceDraft(name="x", connector_kind="local_fs", config={})
        )

    # 报错必须指出是哪个字段, 否则用户只知道"配置不对"
    assert "root_path" in (excinfo.value.details or "")


async def test_bad_config_leaves_no_row_behind(
    service: ResourceService, sessions: async_sessionmaker[AsyncSession]
) -> None:
    with pytest.raises(InvalidConnectorConfigError):
        await service.create(
            ResourceDraft(name="x", connector_kind="local_fs", config={})
        )

    async with sessions() as session:
        assert (await session.execute(select(Resource))).scalars().all() == []


# --------------------------------------------------------------------------- #
# 凭据
# --------------------------------------------------------------------------- #
async def test_secret_goes_to_the_store_not_the_database(
    service: ResourceService,
    credentials: InMemoryCredentialStore,
    sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """数据库会被备份、被同步、被用 sqlite3 直接打开看。"""

    resource = await service.create(
        ResourceDraft(
            name="secret-source",
            connector_kind="local_fs",
            config={"root_path": str(tmp_path)},
            secret=SecretStr("top-secret-token"),
        )
    )

    assert resource.identity_ref == f"keyring:{resource.id}"

    async with sessions() as session:
        row = await session.get(Resource, resource.id)
        assert row is not None
        assert "top-secret-token" not in str(row.config)
        assert "top-secret-token" not in (row.identity_ref or "")

    stored = credentials.get(CredentialRef.for_resource(resource.id))
    assert stored.get_secret_value() == "top-secret-token"


async def test_env_identity_ref_is_stored_verbatim(
    service: ResourceService, tmp_path: Path
) -> None:
    resource = await service.create(
        ResourceDraft(
            name="env-source",
            connector_kind="local_fs",
            config={"root_path": str(tmp_path)},
            identity_ref="env:MY_TOKEN",
        )
    )

    assert resource.identity_ref == "env:MY_TOKEN"


async def test_secret_with_readonly_ref_is_rejected(
    service: ResourceService, tmp_path: Path
) -> None:
    """环境变量由用户自己设置, Shovel 不会去写它 —— 别假装能写。"""

    with pytest.raises(InvalidConnectorConfigError):
        await service.create(
            ResourceDraft(
                name="conflict",
                connector_kind="local_fs",
                config={"root_path": str(tmp_path)},
                secret=SecretStr("x"),
                identity_ref="env:MY_TOKEN",
            )
        )


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
async def test_verify_promotes_to_active(
    service: ResourceService, draft: ResourceDraft
) -> None:
    resource = await service.create(draft)

    result = await service.verify(resource.id)

    assert result.ok
    refreshed = await service.get(resource.id)
    assert refreshed.state is ResourceState.ACTIVE
    assert refreshed.last_verified_at is not None


async def test_verify_failure_degrades_without_raising(
    service: ResourceService, tmp_path: Path
) -> None:
    """连不上是常态, 不该让 CLI 命令以栈回溯收场。"""

    resource = await service.create(
        ResourceDraft(
            name="missing",
            connector_kind="local_fs",
            config={"root_path": str(tmp_path / "gone")},
        )
    )

    result = await service.verify(resource.id)

    assert not result.ok
    assert (await service.get(resource.id)).state is ResourceState.DEGRADED


async def test_strict_verify_raises_on_failure(
    service: ResourceService, tmp_path: Path
) -> None:
    resource = await service.create(
        ResourceDraft(
            name="missing",
            connector_kind="local_fs",
            config={"root_path": str(tmp_path / "gone")},
        )
    )

    with pytest.raises(ResourceVerificationFailedError):
        await service.verify(resource.id, strict=True)

    # 即便抛了异常, 状态也必须已经被落盘
    assert (await service.get(resource.id)).state is ResourceState.DEGRADED


async def test_failed_verify_keeps_the_previous_watermark(
    service: ResourceService, tmp_path: Path
) -> None:
    """保留上一次成功的时间, UI 才能说出'最后一次连通是三天前'。"""

    root = tmp_path / "data"
    root.mkdir()

    resource = await service.create(
        ResourceDraft(name="flaky", connector_kind="local_fs", config={"root_path": str(root)})
    )
    await service.verify(resource.id)
    first = (await service.get(resource.id)).last_verified_at

    root.rmdir()
    await service.verify(resource.id)
    after = await service.get(resource.id)

    assert after.state is ResourceState.DEGRADED
    assert after.last_verified_at == first


async def test_verify_is_repeatable(service: ResourceService, draft: ResourceDraft) -> None:
    """active -> active 是 noop, 不该因为'非法自转移'而炸掉。"""

    resource = await service.create(draft)

    await service.verify(resource.id)
    await service.verify(resource.id)

    assert (await service.get(resource.id)).state is ResourceState.ACTIVE


async def test_verify_can_recover_a_degraded_resource(
    service: ResourceService, tmp_path: Path
) -> None:
    root = tmp_path / "later"

    resource = await service.create(
        ResourceDraft(name="later", connector_kind="local_fs", config={"root_path": str(root)})
    )
    await service.verify(resource.id)
    assert (await service.get(resource.id)).state is ResourceState.DEGRADED

    root.mkdir()
    (root / "a.md").write_text("x", encoding="utf-8")
    await service.verify(resource.id)

    assert (await service.get(resource.id)).state is ResourceState.ACTIVE


# --------------------------------------------------------------------------- #
# 修改
# --------------------------------------------------------------------------- #
async def test_changing_config_falls_back_to_draft(
    service: ResourceService, draft: ResourceDraft, tmp_path: Path
) -> None:
    """旧配置的体检报告不能给新配置背书。"""

    resource = await service.create(draft)
    await service.verify(resource.id)
    assert (await service.get(resource.id)).state is ResourceState.ACTIVE

    other = tmp_path / "other"
    other.mkdir()
    updated = await service.update_config(resource.id, {"root_path": str(other)})

    assert updated.state is ResourceState.DRAFT
    assert updated.last_verified_at is None


async def test_metadata_changes_do_not_require_reverification(
    service: ResourceService, draft: ResourceDraft
) -> None:
    """把描述改个字, 没理由让资源掉出 ACTIVE。"""

    resource = await service.create(draft)
    await service.verify(resource.id)

    updated = await service.update_metadata(resource.id, description="我的工作笔记")

    assert updated.state is ResourceState.ACTIVE
    assert updated.description == "我的工作笔记"
    assert updated.last_verified_at is not None


async def test_out_of_range_authority_is_rejected(
    service: ResourceService, draft: ResourceDraft
) -> None:
    resource = await service.create(draft)

    with pytest.raises(InvalidConnectorConfigError):
        await service.update_metadata(resource.id, authority=42.0)


# --------------------------------------------------------------------------- #
# 状态流转
# --------------------------------------------------------------------------- #
async def test_pause_and_resume(service: ResourceService, draft: ResourceDraft) -> None:
    resource = await service.create(draft)
    await service.verify(resource.id)

    paused = await service.pause(resource.id, "出差中")
    assert paused.state is ResourceState.PAUSED
    assert paused.state_reason == "出差中"

    resumed = await service.resume(resource.id)
    assert resumed.state is ResourceState.ACTIVE


async def test_cannot_pause_a_draft(service: ResourceService, draft: ResourceDraft) -> None:
    resource = await service.create(draft)

    with pytest.raises(InvalidResourceStateTransitionError):
        await service.pause(resource.id)


async def test_revoke_is_terminal(service: ResourceService, draft: ResourceDraft) -> None:
    resource = await service.create(draft)
    await service.verify(resource.id)
    await service.revoke(resource.id)

    assert (await service.get(resource.id)).state is ResourceState.REVOKED

    # 吊销之后不能复活, 也不能重新校验
    with pytest.raises(InvalidResourceStateTransitionError):
        await service.resume(resource.id)

    with pytest.raises(InvalidResourceStateTransitionError):
        await service.verify(resource.id)


async def test_revoke_destroys_the_secret(
    service: ResourceService, credentials: InMemoryCredentialStore, tmp_path: Path
) -> None:
    """吊销的语义就是'这个凭据不该再被 Shovel 持有了'。"""

    resource = await service.create(
        ResourceDraft(
            name="revoke-me",
            connector_kind="local_fs",
            config={"root_path": str(tmp_path)},
            secret=SecretStr("token"),
        )
    )

    await service.revoke(resource.id)

    with pytest.raises(CredentialNotFoundError):
        credentials.get(CredentialRef.for_resource(resource.id))


async def test_revoke_does_not_touch_env_credentials(
    service: ResourceService, credentials: InMemoryCredentialStore, tmp_path: Path
) -> None:
    """env: 引用指向用户自己的环境变量, Shovel 无权删它。"""

    ref = CredentialRef.parse("env:MY_TOKEN")
    credentials.set(ref, SecretStr("from-user"))

    resource = await service.create(
        ResourceDraft(
            name="env-backed",
            connector_kind="local_fs",
            config={"root_path": str(tmp_path)},
            identity_ref="env:MY_TOKEN",
        )
    )
    await service.revoke(resource.id)

    assert credentials.get(ref).get_secret_value() == "from-user"


async def test_config_cannot_be_changed_after_revoke(
    service: ResourceService, draft: ResourceDraft, tmp_path: Path
) -> None:
    resource = await service.create(draft)
    await service.revoke(resource.id)

    with pytest.raises(InvalidResourceStateTransitionError):
        await service.update_config(resource.id, {"root_path": str(tmp_path)})


# --------------------------------------------------------------------------- #
# 查询与删除
# --------------------------------------------------------------------------- #
async def test_resolve_accepts_both_id_and_name(
    service: ResourceService, draft: ResourceDraft
) -> None:
    resource = await service.create(draft)

    assert (await service.resolve(resource.id)).id == resource.id
    assert (await service.resolve("notes")).id == resource.id


async def test_missing_resource_raises(service: ResourceService) -> None:
    with pytest.raises(ResourceNotFoundError):
        await service.resolve("nope")


async def test_list_filters(service: ResourceService, tmp_path: Path) -> None:
    a = await service.create(
        ResourceDraft(name="a", connector_kind="local_fs", config={"root_path": str(tmp_path)})
    )
    await service.create(
        ResourceDraft(
            name="b",
            connector_kind="local_fs",
            config={"root_path": str(tmp_path / "missing")},
        )
    )
    await service.verify(a.id)

    assert len(await service.list()) == 2
    assert [r.name for r in await service.list(state=ResourceState.ACTIVE)] == ["a"]
    assert len(await service.list(connector_kind="local_fs")) == 2
    assert await service.list(connector_kind="notion") == []


async def test_delete_cascades_to_profiles(
    service: ResourceService,
    sessions: async_sessionmaker[AsyncSession],
    draft: ResourceDraft,
) -> None:
    """靠外键 ON DELETE CASCADE —— 前提是 PRAGMA foreign_keys 真的开了。"""

    resource = await service.create(draft)
    await service.delete(resource.id)

    async with sessions() as session:
        result = await session.execute(
            select(ScanProfile).where(ScanProfile.resource_id == resource.id)
        )
        assert result.scalars().all() == []

    with pytest.raises(ResourceNotFoundError):
        await service.get(resource.id)


async def test_delete_clears_the_secret(
    service: ResourceService, credentials: InMemoryCredentialStore, tmp_path: Path
) -> None:
    resource = await service.create(
        ResourceDraft(
            name="delete-me",
            connector_kind="local_fs",
            config={"root_path": str(tmp_path)},
            secret=SecretStr("token"),
        )
    )

    await service.delete(resource.id)

    with pytest.raises(CredentialNotFoundError):
        credentials.get(CredentialRef.for_resource(resource.id))


# --------------------------------------------------------------------------- #
# build_connector
# --------------------------------------------------------------------------- #
async def test_build_connector_returns_a_usable_instance(
    service: ResourceService, draft: ResourceDraft, tmp_path: Path
) -> None:
    (tmp_path / "a.md").write_text("hello", encoding="utf-8")

    resource = await service.create(draft)
    connector = await service.build_connector(resource.id)

    result = await connector.verify()
    assert result.ok

    from shovel.services.connector_base import DiscoveryScope

    ids = {item.external_id async for item in connector.discover(DiscoveryScope())}
    assert ids == {"a.md"}


async def test_build_connector_resolves_the_secret(
    service: ResourceService, credentials: InMemoryCredentialStore, tmp_path: Path
) -> None:
    """凭据解析必须发生在这一层, 否则总有调用方忘了解然后得到一个 401。"""

    resource = await service.create(
        ResourceDraft(
            name="with-secret",
            connector_kind="local_fs",
            config={"root_path": str(tmp_path)},
            secret=SecretStr("token"),
        )
    )

    # local_fs 不用凭据, 但解析这一步仍然要跑通 —— 密钥缺失会在这里抛
    connector = await service.build_connector(resource.id)
    assert (await connector.verify()).ok

    credentials.delete(CredentialRef.for_resource(resource.id))
    with pytest.raises(CredentialNotFoundError):
        await service.build_connector(resource.id)
