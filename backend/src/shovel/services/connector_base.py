#---------------------------------------------------------
# 连接器协议与能力声明
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""Connector 是"怎么连上一个数据源、怎么把里面的东西列出来"的抽象。

## 与 Resource 表的分工

``Resource`` 表存的是**配置**: 连哪个目录、用哪个凭据、权威度多少。
Connector 是**代码**: 怎么连、怎么列举、怎么读。

这就是 ``Resource.connector_kind`` 刻意不做外键的原因 —— 它是进本模块
注册表的 key, 而注册表里装的是类, 不是数据行。规则是一句话:
**配置进表, 能力进注册表。**

## 为什么用 Protocol 而不是 ABC

连接器未来会来自插件 (第三方包、甚至用户自己写的一个文件)。强制继承一个
基类, 意味着插件作者必须 import shovel 的内部模块才能被识别; 用 Protocol
则只要长得对就行。代价是拿不到基类提供的默认实现 —— 但连接器本来就几乎
没有可复用的默认行为, 这个代价是零。

## 为什么 Connector 不碰数据库

Connector 收到的是已经解出来的 config 与 credential, 返回的是纯数据结构。
它不认识 Session, 不认识 Resource ORM 对象。这样做的直接好处是测试:
测一个连接器只需要一个临时目录, 不需要建库、建表、塞一行资源。

## 本轮的边界

``discover()`` 的签名在这里定死, 但本轮**没有消费者** —— 扫描 pipeline 是
下一个模块的事。这是刻意的: 让扫描模块开工时接口已经稳定, 而不是一边写
pipeline 一边改协议。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, SecretStr

from shovel.domain.enums import DocClass, Modality, ScanMode, Sensitivity


class ConnectorCapability(StrEnum):
    """连接器能做什么。

    能力不是装饰性的元数据, 它直接决定调度器允许配哪些 ``ScanMode``:
    一个没有 ``INCREMENTAL`` 的连接器, 配上增量调度只会得到一个
    每次都全量重扫、却自称"增量"的任务。与其让用户在运行时才发现,
    不如在创建 ScanProfile 时就拒绝。
    """

    #: 能列举出数据源里有哪些条目。这是最低要求, 任何连接器都必须有。
    DISCOVER = "discover"

    #: 能只列举"自上次以来变过的"。没有这个能力就只能 FULL_SWEEP。
    INCREMENTAL = "incremental"

    #: 能被动接收变更通知(文件系统事件、webhook), 而不是靠轮询。
    WATCH = "watch"

    #: 能按条目单独取内容。没有这个能力的连接器只能在 discover 时
    #: 把内容一并带出来(例如某些一次性导出的 API)。
    RANDOM_READ = "random_read"

    #: 全量扫描的结果可以被信任为"完整列表", 因而能用差集推断删除。
    #: 一个分页 API 如果不保证遍历期间的一致性, 就不该声明这个能力 ——
    #: 否则一次漏页会被当成"用户删了一批文档"。
    DELETE_DETECT = "delete_detect"


#: 能力 -> 该能力解锁的扫描模式。
#: TARGETED 不在这里: 它是"用户点名扫这几个", 任何能 DISCOVER 的连接器都支持。
_MODE_REQUIREMENTS: dict[ScanMode, ConnectorCapability] = {
    ScanMode.FULL_SWEEP: ConnectorCapability.DISCOVER,
    ScanMode.INCREMENTAL: ConnectorCapability.INCREMENTAL,
    ScanMode.TARGETED: ConnectorCapability.DISCOVER,
}


@dataclass(frozen=True, slots=True)
class ConnectorSpec:
    """一个连接器的静态描述。

    ``config_model`` 是这里最重要的字段: 一处 pydantic 声明, 同时服务于
    三个场景 —— service 层的配置校验、CLI 的字段提示、未来 Studio 的表单
    生成。如果改成手写 dict schema, 这三处就会各自漂移。
    """

    kind: str
    display_name: str
    description: str
    config_model: type[BaseModel]
    capabilities: frozenset[ConnectorCapability]

    #: 这个连接器是否必须配凭据。local_fs 不需要, 任何云 API 都需要。
    requires_identity: bool = False

    #: 新建资源时 authority / sensitivity 的默认值。
    #: 放在连接器上而不是写死在 service 里, 是因为"本地笔记目录"和
    #: "公开网页抓取"的可信度天然不同, 而只有连接器知道自己是哪种。
    default_authority: float = 0.6
    default_sensitivity: Sensitivity = Sensitivity.NORMAL

    def supports(self, capability: ConnectorCapability) -> bool:
        return capability in self.capabilities

    def supports_mode(self, mode: ScanMode) -> bool:
        required = _MODE_REQUIREMENTS[mode]
        return required in self.capabilities

    def supported_modes(self) -> frozenset[ScanMode]:
        return frozenset(m for m in ScanMode if self.supports_mode(m))

    def default_scan_mode(self) -> ScanMode:
        """建默认 ScanProfile 时挑哪个模式。

        能增量就增量 —— 全量扫描在用户的工作机上是最贵的动作,
        只有在连接器压根不支持增量时才退回去。
        """

        if self.supports(ConnectorCapability.INCREMENTAL):
            return ScanMode.INCREMENTAL
        return ScanMode.FULL_SWEEP

    def __post_init__(self) -> None:
        if ConnectorCapability.DISCOVER not in self.capabilities:
            raise ValueError(
                f"连接器 '{self.kind}' 必须声明 DISCOVER 能力: "
                "一个列举不出任何条目的数据源没有存在意义。"
            )

        if not 0.0 <= self.default_authority <= 1.0:
            raise ValueError(
                f"连接器 '{self.kind}' 的 default_authority "
                f"必须落在 [0.0, 1.0], 实际是 {self.default_authority}。"
            )


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """一次连通性校验的结果。

    ``ok=False`` 是**正常返回值**, 不是异常。原因: 校验失败是资源的一种
    常态 (网线拔了、目录被删了、token 过期了), 而常态不该用异常表达 ——
    service 层要做的是把它写进 DEGRADED 状态, 而不是让整个 CLI 命令崩掉。

    真正的异常 (代码 bug) 仍然照常往上抛。
    """

    ok: bool
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)
    latency_ms: int | None = None

    @classmethod
    def success(
        cls,
        message: str = "连接正常。",
        *,
        details: Mapping[str, Any] | None = None,
        latency_ms: int | None = None,
    ) -> VerifyResult:
        return cls(
            ok=True,
            message=message,
            details=dict(details or {}),
            latency_ms=latency_ms,
        )

    @classmethod
    def failure(
        cls,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        latency_ms: int | None = None,
    ) -> VerifyResult:
        return cls(
            ok=False,
            message=message,
            details=dict(details or {}),
            latency_ms=latency_ms,
        )


@dataclass(frozen=True, slots=True)
class DiscoveredItem:
    """``discover()`` 吐出的一个条目。

    注意它**不含内容**。discover 的职责是"列出有什么", 取内容是 ``open()``
    的事。把两者分开, 是为了让"列举 20 万个文件"这个动作能在几秒内完成并
    与已有的 Document 表做差集 —— 如果 discover 顺带读盘, 一次增量扫描就得
    把整个知识库读一遍, 增量也就没意义了。
    """

    #: 在该资源内稳定且唯一的标识。**不要用绝对路径** ——
    #: 详见 local_fs 连接器里关于"根目录改名"的说明。
    external_id: str

    #: 可定位到原件的 URI, 给人看、给 agent 引用用。
    uri: str

    title: str | None = None
    size_bytes: int | None = None

    #: 源端的最后修改时间 (unix epoch 秒)。增量扫描的主要依据。
    modified_at: int | None = None

    #: 内容指纹。相同表示无需重新解析 —— 这是比 modified_at 更可靠的
    #: "变没变"判据, 因为很多系统会在内容没变时刷新 mtime。
    content_hash: str | None = None

    doc_class: DocClass = DocClass.UNKNOWN
    modality: Modality = Modality.TEXT

    #: 连接器特有的附加信息, 原样存进 Document 的 metadata。
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveryScope:
    """discover() 的过滤条件。

    这是 ``ScanProfile`` 的一个**只读投影**, 而不是直接把 ORM 对象传进来。
    为什么多这一层:

    * 连接器因此不必 import ORM 模型, 测试时构造一个 scope 就够了。
    * TARGETED 模式下根本没有 profile, 只有一串用户点名的 id —— 用同一个
      dataclass 表达两种来源, 连接器就只需要一条代码路径。
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_file_bytes: int | None = None

    #: 只要最近 N 天内修改过的。None 表示不限。
    time_window_days: int | None = None

    #: TARGETED 模式下用户点名的 external_id。非空时其余过滤条件仍然生效。
    target_ids: tuple[str, ...] = ()

    #: 增量模式的水位线: 只要严格晚于这个时间戳的。
    since: int | None = None

    @classmethod
    def from_profile(cls, profile: Any, *, since: int | None = None,
                     target_ids: Iterable[str] = ()) -> DiscoveryScope:
        """从一个 ``ScanProfile`` ORM 对象投影出 scope。

        用 duck typing 而不是 import ScanProfile: 保持"连接器层不依赖 db 层"
        这条边界, 顺便让测试可以塞一个简单的 stub 进来。
        """

        return cls(
            include=tuple(profile.include),
            exclude=tuple(profile.exclude),
            max_file_bytes=profile.max_file_bytes,
            time_window_days=profile.time_window_days,
            target_ids=tuple(target_ids),
            since=since,
        )


class Connector(Protocol):
    """所有连接器必须长成的样子。

    实现约定:

    * ``__init__(config, credential)`` —— config 是已经过 ``spec.config_model``
      校验的实例, credential 是解出来的明文 (或 None)。连接器不负责解析,
      也不负责校验, 拿到手的东西就是可用的。
    * 所有方法都是 async。即使 local_fs 这种纯同步的实现也要 async ——
      统一的签名让调用方不必为两种连接器写两套代码。

    **不要继承它。** 显式继承一个 Protocol 会把这里的 ``...`` 当成默认实现
    继承下去: 某个连接器忘了写 ``verify``, 得到的不是报错, 而是一个永远
    返回 ``None`` 的 verify。只实现、不继承, 漏掉的方法才会在调用时立刻暴露。

    同理没有加 ``@runtime_checkable``: 带数据成员 (``spec``) 的 Protocol
    做 ``isinstance`` 会直接抛 TypeError, 那个能力在这里既用不上也不安全。
    """

    spec: ClassVar[ConnectorSpec]

    def __init__(
        self,
        config: BaseModel,
        credential: SecretStr | None = None,
    ) -> None: ...

    async def verify(self) -> VerifyResult:
        """连通性与权限检查。失败返回 ok=False, 不抛异常。"""
        ...

    def discover(self, scope: DiscoveryScope) -> AsyncIterator[DiscoveredItem]:
        """列举条目。

        返回 AsyncIterator 而不是 list: 一个资源可能有几十万个条目,
        全部装进内存再返回, 在用户的笔记本上会直接把内存吃满。
        """
        ...

    def open(self, item: DiscoveredItem) -> AsyncIterator[bytes]:
        """取一个条目的原始字节流。

        只有声明了 ``RANDOM_READ`` 能力的连接器才需要实现;
        其余实现应抛 :class:`ConnectorCapabilityError`。
        """
        ...
