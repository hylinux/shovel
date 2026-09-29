#---------------------------------------------------------
# 连接器注册表
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""``connector_kind`` -> 连接器类 的查找表。

## 为什么是注册表而不是数据库表

连接器是代码。把它写进数据库, 意味着"系统支持哪些数据源"这件事可以被
一条 UPDATE 改掉, 而代码却没变 —— 于是就会出现"表里写着支持 notion,
但进程里根本没有 notion 的实现"这种无法自愈的状态。

注册表则相反: 它的内容由"哪些模块被 import 了"唯一决定, 和磁盘上的
数据无关。资源表里那个 ``connector_kind`` 字符串如果在注册表里查不到,
答案永远是同一个 —— 这个版本的 Shovel 不认识它。

## import 即注册

``register_builtin_connectors()`` 负责把内置连接器导进来。放在函数里而不是
模块顶层, 是为了避免循环 import: 连接器模块要 import 本模块拿 ``@register``,
本模块又要 import 连接器模块 —— 顶层写就是死循环。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, cast

from shovel.exceptions.resource import (
    DuplicateConnectorError,
    UnknownConnectorError,
)

if TYPE_CHECKING:
    from shovel.services.connector_base import Connector, ConnectorSpec


#: kind -> 连接器类。模块级单例。
_REGISTRY: dict[str, type[Connector]] = {}

#: 注册发生在 import 期间, 而 import 在多线程下可能并发 (例如 CLI 与后台
#: 扫描线程同时首次触碰本模块)。一把锁的成本可以忽略, 而少了它就可能
#: 出现两个线程同时判断"还没注册过"然后都写进去。
_LOCK = threading.RLock()

#: 内置连接器是否已经导入过。用标志位而不是"检查字典非空",
#: 因为测试会往空注册表里塞 fake 连接器, 那种情况不该被误认为"已初始化"。
_BUILTINS_LOADED = False


def register[C: type](cls: C) -> C:
    """把一个连接器类注册进表。用作装饰器。

    重复的 kind 直接抛异常而不是后者覆盖前者。覆盖是最糟的选择:
    最终生效的是哪个实现, 会取决于 import 顺序, 而 import 顺序是
    没人会去推理的东西。

    入参故意是裸 ``type`` 而不是 ``type[Connector]``: 连接器是鸭子类型的,
    "算不算连接器"由下面这段运行时检查(有没有 ``spec``)决定, 而不是由
    继承关系决定。测试里的 fake 连接器也因此不必继承任何基类。
    """

    spec = getattr(cls, "spec", None)
    if spec is None:
        raise TypeError(
            f"{cls.__qualname__} 没有 'spec' 类属性, 不能注册为连接器。"
        )

    kind = spec.kind

    with _LOCK:
        existing = _REGISTRY.get(kind)
        if existing is not None and existing is not cls:
            raise DuplicateConnectorError(
                kind,
                existing.__qualname__,
                cls.__qualname__,
            )

        _REGISTRY[kind] = cast("type[Connector]", cls)

    return cls


def get(kind: str) -> type[Connector]:
    """按 kind 取连接器类; 查不到抛 :class:`UnknownConnectorError`。"""

    _ensure_builtins()

    with _LOCK:
        cls = _REGISTRY.get(kind)
        if cls is None:
            raise UnknownConnectorError(kind, tuple(_REGISTRY))

    return cls


def get_spec(kind: str) -> ConnectorSpec:
    spec: ConnectorSpec = get(kind).spec
    return spec


def has(kind: str) -> bool:
    _ensure_builtins()
    with _LOCK:
        return kind in _REGISTRY


def kinds() -> tuple[str, ...]:
    """所有已注册的 kind, 按字母序。"""

    _ensure_builtins()
    with _LOCK:
        return tuple(sorted(_REGISTRY))


def all_specs() -> tuple[ConnectorSpec, ...]:
    """所有连接器的 spec, 按 kind 排序。CLI 和 Studio 的目录页用它。"""

    _ensure_builtins()
    with _LOCK:
        items = sorted(_REGISTRY.items())

    return tuple(cls.spec for _, cls in items)


def iter_specs() -> Iterator[ConnectorSpec]:
    yield from all_specs()


def _ensure_builtins() -> None:
    """惰性导入内置连接器。

    惰性而非 eager, 是为了让"只 import 注册表"的代码 (例如类型检查、
    文档生成) 不必把所有连接器的依赖也一起拖进来。
    """

    global _BUILTINS_LOADED

    with _LOCK:
        if _BUILTINS_LOADED:
            return
        # 先置位再 import: 连接器模块顶层会回头调用 register(),
        # 那次调用会重入本模块, 标志位没先置位就会无限递归。
        _BUILTINS_LOADED = True

    try:
        register_builtin_connectors()
    except Exception:
        with _LOCK:
            _BUILTINS_LOADED = False
        raise


def register_builtin_connectors() -> None:
    """导入随 Shovel 一起发布的连接器并把它们登记进表。

    新增内置连接器时, 在这里加一行 import + 一次 ``register`` 调用。

    为什么是显式 ``register(...)`` 而不是只靠类上的 ``@register``:
    装饰器只在模块**第一次**被 import 时执行。测试清空注册表之后再走这条
    路, 模块已经躺在 ``sys.modules`` 里, import 语句是个空操作, 装饰器不会
    再跑一遍 —— 注册表会保持空的。显式调用则是幂等的 (同一个类重复注册
    不报错), 无论模块是不是新导入的都能把表填回去。
    """

    from shovel.services.connector_local_fs import LocalFsConnector

    register(LocalFsConnector)


# --------------------------------------------------------------------------- #
# 测试辅助
# --------------------------------------------------------------------------- #
def _reset_for_tests(
    *,
    load_builtins: bool = False,
    factory: Callable[[], None] | None = None,
) -> None:
    """清空注册表。**只给测试用。**

    放在这里而不是让测试直接改 ``_REGISTRY``, 是为了让"注册表是私有状态"
    这件事仍然成立 —— 测试有一个受支持的入口, 而不是各自去戳内部变量。
    """

    global _BUILTINS_LOADED

    with _LOCK:
        _REGISTRY.clear()
        _BUILTINS_LOADED = not load_builtins

    if factory is not None:
        factory()

    if load_builtins:
        _ensure_builtins()
