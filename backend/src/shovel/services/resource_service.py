#---------------------------------------------------------
# 资源服务层
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""资源的增删改查、校验与状态流转。

这一层是**唯一**同时认识数据库、注册表和凭据库的地方。
分工是这样的:

```text
Connector        怎么连、怎么列举        不认识 Session
CredentialStore  秘密存在哪              不认识 Resource
state_machine    哪些状态能转到哪些      不认识任何 IO
ResourceService  把上面三个缝起来        <- 就是本模块
```

把缝合逻辑收在一处, 换来的是: CLI、未来的 HTTP API、以及扫描调度器
调的是同一套方法, 因而不可能出现"CLI 改状态会写 state_reason, 而 API
不会"这种分叉。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, SecretStr, ValidationError
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.db.base import now_ts
from shovel.db.model.config import Resource, ScanProfile
from shovel.domain.enums import ResourceState, ScanMode, Sensitivity
from shovel.domain.resource_state import (
    allowed_targets,
    assert_transition,
    is_terminal,
    plan_transition,
)
from shovel.exceptions.resource import (
    DuplicateResourceNameError,
    InvalidConnectorConfigError,
    InvalidResourceStateTransitionError,
    ResourceNotFoundError,
    ResourceVerificationFailedError,
)
from shovel.services import connector_registry
from shovel.services.connector_base import Connector, ConnectorSpec, VerifyResult
from shovel.services.credentials import (
    CredentialRef,
    CredentialStore,
    build_credential_store,
)

#: 默认 ScanProfile 的名字。固定而不是随机, 这样"每个资源都有一个叫
#: default 的 profile"成为一条可以依赖的约定。
DEFAULT_PROFILE_NAME = "default"


@dataclass(frozen=True, slots=True)
class ResourceDraft:
    """创建一个资源所需的全部输入。

    用一个 dataclass 而不是 10 个关键字参数: 这组字段会被 CLI、HTTP API
    和测试共同构造, 有个具名类型可以让三处共享同一套默认值和校验。
    """

    name: str
    connector_kind: str
    config: dict[str, Any]

    description: str | None = None
    sensitivity: Sensitivity | None = None
    authority: float | None = None

    #: 明文密钥。会被写进凭据库, **不会**进数据库。
    secret: SecretStr | None = None

    #: 直接指定引用 (例如 ``env:MY_TOKEN``)。与 ``secret`` 互斥 ——
    #: 前者是"密钥已经在别处了", 后者是"请帮我存起来"。
    identity_ref: str | None = None


class ResourceService:
    """资源的应用服务。

    每个公开方法自己开一个 session。刻意不做成"构造时持有一个 session":
    CLI 的一条命令就是一次完整的业务操作, 让它自然对应一个事务边界,
    比让调用方记得什么时候 commit 要可靠得多。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        credentials: CredentialStore | None = None,
    ) -> None:
        self._sessions = session_factory
        self._credentials = credentials or build_credential_store()

    # ----------------------------------------------------------------- #
    # 查询
    # ----------------------------------------------------------------- #
    async def get(self, resource_id: str) -> Resource:
        async with self._sessions() as session:
            resource = await session.get(Resource, resource_id)

        if resource is None:
            raise ResourceNotFoundError(resource_id)

        return resource

    async def get_by_name(self, name: str) -> Resource:
        async with self._sessions() as session:
            resource = await self._find_by_name(session, name)

        if resource is None:
            raise ResourceNotFoundError(name)

        return resource

    async def resolve(self, ref: str) -> Resource:
        """按 id 或名字找资源。

        CLI 里用户输的几乎总是名字, 脚本里传的几乎总是 id。
        与其让每个调用点自己试两次, 不如在这里试。
        """

        async with self._sessions() as session:
            resource = await session.get(Resource, ref)
            if resource is None:
                resource = await self._find_by_name(session, ref)

        if resource is None:
            raise ResourceNotFoundError(ref)

        return resource

    async def list(
        self,
        *,
        state: ResourceState | None = None,
        connector_kind: str | None = None,
    ) -> Sequence[Resource]:
        stmt = select(Resource).order_by(Resource.name)

        if state is not None:
            stmt = stmt.where(Resource.state == state)

        if connector_kind is not None:
            stmt = stmt.where(Resource.connector_kind == connector_kind)

        async with self._sessions() as session:
            result = await session.execute(stmt)
            return result.scalars().all()

    # ----------------------------------------------------------------- #
    # 创建与修改
    # ----------------------------------------------------------------- #
    async def create(self, draft: ResourceDraft) -> Resource:
        """新建一个资源, 初始状态 DRAFT。

        为什么新建出来是 DRAFT 而不是直接 ACTIVE: 创建只证明"配置的形状
        对了", 不证明"这个目录真的存在且可读"。只有 ``verify()`` 能证明
        后者。一个从未被校验过却显示 ACTIVE 的资源, 会让调度器把它排进
        扫描队列, 然后在真正跑的时候才发现连不上。
        """

        spec = connector_registry.get_spec(draft.connector_kind)
        config_model = self._validate_config(spec, draft.config)

        identity_ref = self._resolve_identity_ref(draft)

        resource = Resource(
            name=draft.name,
            connector_kind=spec.kind,
            config=config_model.model_dump(mode="json"),
            state=ResourceState.DRAFT,
            state_reason="资源刚创建, 尚未校验。",
            sensitivity=draft.sensitivity or spec.default_sensitivity,
            authority=(
                draft.authority
                if draft.authority is not None
                else spec.default_authority
            ),
            description=draft.description or spec.description,
        )

        async with self._sessions() as session:
            if await self._find_by_name(session, draft.name) is not None:
                raise DuplicateResourceNameError(draft.name)

            session.add(resource)
            # flush 而不是 commit: 需要先拿到自动生成的 id 才能算出
            # keyring 的 account, 而密钥写失败时整行都不该留下。
            await session.flush()

            if draft.secret is not None:
                ref = (
                    CredentialRef.parse(identity_ref)
                    if identity_ref
                    else CredentialRef.for_resource(resource.id)
                )
                # 先写凭据再 commit。反过来的话, 凭据写失败就会留下一行
                # "指向一个不存在的密钥"的资源, 而那种半成品状态没有
                # 任何自动修复的路径。
                self._credentials.set(ref, draft.secret)
                resource.identity_ref = str(ref)
            elif identity_ref:
                resource.identity_ref = identity_ref

            session.add(
                self._build_default_profile(resource_id=resource.id, spec=spec)
            )

            await session.commit()
            await session.refresh(resource)

        return resource

    async def update_config(
        self,
        ref: str,
        config: dict[str, Any],
    ) -> Resource:
        """替换资源的连接配置。

        改完之后**强制回落 DRAFT**。理由: ``last_verified_at`` 证明的是
        "旧配置在那个时刻是通的", 它对新配置不构成任何证据。留在 ACTIVE
        等于用旧配置的体检报告给新配置背书。
        """

        resource = await self.resolve(ref)
        spec = connector_registry.get_spec(resource.connector_kind)
        config_model = self._validate_config(spec, config)

        async with self._sessions() as session:
            row = await self._load(session, resource.id)

            if is_terminal(row.state):
                raise InvalidResourceStateTransitionError(
                    row.name, row.state.value, ResourceState.DRAFT.value,
                    _values(allowed_targets(row.state)),
                )

            row.config = config_model.model_dump(mode="json")

            if row.state is not ResourceState.DRAFT:
                assert_transition(
                    row.state, ResourceState.DRAFT, resource_ref=row.name,
                )
                row.state = ResourceState.DRAFT

            row.state_reason = "连接配置已修改, 需要重新校验。"
            # 旧的校验结果对新配置无效, 清掉而不是留着误导人
            row.last_verified_at = None

            await session.commit()
            await session.refresh(row)
            return row

    async def update_metadata(
        self,
        ref: str,
        *,
        description: str | None = None,
        sensitivity: Sensitivity | None = None,
        authority: float | None = None,
    ) -> Resource:
        """改那些不影响"能不能连上"的字段。

        与 ``update_config`` 分开, 正是因为这些字段改了**不需要**重新校验 ——
        把描述从"我的笔记"改成"工作笔记", 没有任何理由让资源掉出 ACTIVE。
        """

        resource = await self.resolve(ref)

        async with self._sessions() as session:
            row = await self._load(session, resource.id)

            if description is not None:
                row.description = description

            if sensitivity is not None:
                row.sensitivity = sensitivity

            if authority is not None:
                if not 0.0 <= authority <= 1.0:
                    raise InvalidConnectorConfigError(
                        row.connector_kind,
                        f"authority 必须落在 [0.0, 1.0], 收到的是 {authority}。",
                    )
                row.authority = authority

            await session.commit()
            await session.refresh(row)
            return row

    # ----------------------------------------------------------------- #
    # 状态流转
    # ----------------------------------------------------------------- #
    async def verify(self, ref: str, *, strict: bool = False) -> VerifyResult:
        """跑一次连通性校验, 并把结果写进状态。

        成功 -> ACTIVE, 失败 -> DEGRADED。失败**不抛异常**, 因为
        "连不上"是资源的一种常态, 而常态不该让 CLI 命令以栈回溯收场。
        需要"校验不过就中止"的调用方传 ``strict=True``。

        中途会经过 VERIFYING 并 commit 一次。这一次多余的写入是有意的:
        校验可能耗时数秒(网络连接器会更久), 期间另一个进程查到的状态
        应该是"正在校验", 而不是一个过时的 ACTIVE。
        """

        resource = await self.resolve(ref)

        await self._enter_verifying(resource.id)

        connector = await self.build_connector(resource.id)
        result = await connector.verify()

        target = ResourceState.ACTIVE if result.ok else ResourceState.DEGRADED

        async with self._sessions() as session:
            row = await self._load(session, resource.id)

            change = plan_transition(
                row.state,
                target,
                reason=result.message,
                resource_ref=row.name,
            )

            row.state = change.new
            row.state_reason = change.reason

            # 只有成功才刷新水位线。失败时保留上一次成功的时间,
            # 这样 UI 能说出"最后一次连通是三天前"。
            if result.ok:
                row.last_verified_at = now_ts()

            await session.commit()

        if strict and not result.ok:
            raise ResourceVerificationFailedError(resource.name, result.message)

        return result

    async def pause(self, ref: str, reason: str | None = None) -> Resource:
        return await self._transition(
            ref,
            ResourceState.PAUSED,
            reason or "用户手动暂停。",
        )

    async def resume(self, ref: str) -> Resource:
        """从 PAUSED 恢复。

        恢复到 ACTIVE 而不是 VERIFYING: 暂停期间配置没变过, 上一次的
        校验结果仍然是当前配置的有效证据。真想重新体检的用户会直接跑
        ``shovel resource verify``。
        """

        return await self._transition(
            ref,
            ResourceState.ACTIVE,
            "用户手动恢复。",
        )

    async def revoke(self, ref: str, reason: str | None = None) -> Resource:
        """吊销资源: 终态, 并销毁它的凭据。

        为什么删凭据而不是只改状态: 吊销的语义就是"这个凭据不该再被
        Shovel 持有了"。只改状态会让密钥继续躺在系统凭据库里, 而用户
        以为自己已经收回了授权。

        文档**不删**。吊销的是访问权, 不是已经索引过的知识 ——
        真要连内容一起清掉, 那是 ``delete()`` 的事。
        """

        resource = await self._transition(
            ref,
            ResourceState.REVOKED,
            reason or "用户手动吊销。",
        )

        self._forget_secret(resource.identity_ref)
        return resource

    # ----------------------------------------------------------------- #
    # 删除
    # ----------------------------------------------------------------- #
    async def delete(self, ref: str) -> str:
        """彻底删掉一个资源。

        ``scan_profile`` / ``schedule`` 靠外键 ON DELETE CASCADE 跟着走 ——
        前提是 SQLite 的 ``PRAGMA foreign_keys=ON`` 生效, 这一点由
        ``db.database.PRAGMAS`` 保证 (SQLite 默认是 OFF, 忘了开的话级联
        会静默失效, 只留下一堆孤儿行)。

        返回被删资源的 id, 方便调用方把它送进 DeletionOutbox 去清向量。
        """

        resource = await self.resolve(ref)

        async with self._sessions() as session:
            row = await self._load(session, resource.id)
            identity_ref = row.identity_ref

            # 用 Core DELETE 而不是 session.delete(row)。
            #
            # ORM 级联 (relationship 上的 cascade="all, delete-orphan") 需要先把
            # profiles 集合加载出来才能级联, 而在 async session 里那次隐式的
            # 惰性加载会直接抛 MissingGreenlet。这里要的本来就是数据库自己的
            # ON DELETE CASCADE, 绕开 ORM 反而是更直接的表达。
            await session.execute(
                sa_delete(Resource).where(Resource.id == row.id)
            )
            await session.commit()

        self._forget_secret(identity_ref)
        return resource.id

    # ----------------------------------------------------------------- #
    # 连接器装配
    # ----------------------------------------------------------------- #
    async def build_connector(self, resource_id: str) -> Connector:
        """把一行 Resource 变成一个可用的连接器实例。

        这是本模块存在的核心理由: 注册表查类、pydantic 解 config、
        凭据库解 secret —— 三件事缺一不可, 而且顺序固定。让调用方
        自己拼, 迟早会有人忘了解凭据然后得到一个 401。
        """

        resource = await self.get(resource_id)

        connector_cls = connector_registry.get(resource.connector_kind)
        spec: ConnectorSpec = connector_cls.spec

        config = self._validate_config(spec, resource.config)
        credential = self._load_secret(resource)

        return connector_cls(config, credential)

    # ----------------------------------------------------------------- #
    # 内部
    # ----------------------------------------------------------------- #
    async def _transition(
        self,
        ref: str,
        target: ResourceState,
        reason: str,
    ) -> Resource:
        resource = await self.resolve(ref)

        async with self._sessions() as session:
            row = await self._load(session, resource.id)

            assert_transition(row.state, target, resource_ref=row.name)

            row.state = target
            row.state_reason = reason

            await session.commit()
            await session.refresh(row)
            return row

    async def _enter_verifying(self, resource_id: str) -> None:
        """把资源置为 VERIFYING。已经在 VERIFYING 的话什么都不做。

        并发地跑两次 verify 不该让第二次因为"verifying -> verifying 是
        非法自转移"而炸掉 —— 那只是重复劳动, 不是错误。
        """

        async with self._sessions() as session:
            row = await self._load(session, resource_id)

            if row.state is ResourceState.VERIFYING:
                return

            assert_transition(
                row.state, ResourceState.VERIFYING, resource_ref=row.name,
            )

            row.state = ResourceState.VERIFYING
            row.state_reason = "正在校验连通性……"

            await session.commit()

    @staticmethod
    async def _load(session: AsyncSession, resource_id: str) -> Resource:
        row = await session.get(Resource, resource_id)
        if row is None:
            raise ResourceNotFoundError(resource_id)
        return row

    @staticmethod
    async def _find_by_name(session: AsyncSession, name: str) -> Resource | None:
        result = await session.execute(
            select(Resource).where(Resource.name == name)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _validate_config(
        spec: ConnectorSpec,
        config: dict[str, Any],
    ) -> BaseModel:
        """用连接器自己声明的模型校验 config。

        pydantic 的 ValidationError 原样丢给用户会很难读, 但它里面
        "哪个字段、为什么不行"的信息又是必需的 —— 所以包一层,
        把可读的摘要放进 details。
        """

        try:
            return spec.config_model.model_validate(config)
        except ValidationError as exc:
            raise InvalidConnectorConfigError(
                spec.kind,
                _format_validation_error(exc),
            ) from exc

    @staticmethod
    def _resolve_identity_ref(draft: ResourceDraft) -> str | None:
        if draft.secret is not None and draft.identity_ref is not None:
            ref = CredentialRef.parse(draft.identity_ref)
            if not ref.is_writable:
                raise InvalidConnectorConfigError(
                    draft.connector_kind,
                    f"引用 '{ref}' 是只读的, 不能同时提供 secret。"
                    " 环境变量由用户自己设置, Shovel 不会去写它。",
                )
            return str(ref)

        if draft.identity_ref is not None:
            return str(CredentialRef.parse(draft.identity_ref))

        return None

    def _load_secret(self, resource: Resource) -> SecretStr | None:
        if not resource.identity_ref:
            return None

        return self._credentials.get(CredentialRef.parse(resource.identity_ref))

    def _forget_secret(self, identity_ref: str | None) -> None:
        """销毁一个凭据。只读引用 (env:) 跳过 —— 那是用户的环境变量。"""

        if not identity_ref:
            return

        ref = CredentialRef.parse(identity_ref)

        if not ref.is_writable:
            return

        self._credentials.delete(ref)

    @staticmethod
    def _build_default_profile(
        *,
        resource_id: str,
        spec: ConnectorSpec,
    ) -> ScanProfile:
        """给新资源配一个默认 ScanProfile。

        为什么创建资源时就顺手建: 一个没有任何 profile 的资源永远不会被
        扫描, 而用户在 UI 上看到的是一个"ACTIVE 却什么都不做"的东西。
        默认给一个, 用户改它比凭空发现自己少建了一个要容易得多。

        扫描模式由连接器的能力决定, 不写死 —— 见 ``spec.default_scan_mode()``。
        """

        mode: ScanMode = spec.default_scan_mode()

        return ScanProfile(
            resource_id=resource_id,
            name=DEFAULT_PROFILE_NAME,
            scan_mode=mode,
            is_enabled=True,
        )


def _format_validation_error(exc: ValidationError) -> str:
    lines: list[str] = []

    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(根)"
        lines.append(f"  {location}: {error['msg']}")

    return "\n".join(lines)


def _values(states: Iterable[ResourceState]) -> list[str]:
    return [s.value for s in states]
