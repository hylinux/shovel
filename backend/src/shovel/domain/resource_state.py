#---------------------------------------------------------
# 资源状态机
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""``ResourceState`` 的合法转移表。

为什么要把转移规则单独拎出来, 而不是让 service 层各处自己 `if` 一下:

1. **状态有 6 个取值, 组合有 30 种。** 靠调用方自觉检查, 意味着 30 条规则
   散落在 create / verify / pause / resume / revoke 五个方法里, 而且每加一个
   新入口就要重抄一遍。
2. **REVOKED 是终态, 这件事必须只写一次。** 如果"撤销后不能复活"这条规则
   散落在多处, 总有一处会漏掉, 而漏掉的后果是一个已被吊销的凭据又开始扫描。
3. **转移必须留下原因。** `state_reason` 在模型里是可空列, 靠自觉就一定会有
   人忘记填。这里把 reason 做成 `transition()` 的必填参数, 从签名上堵死。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from shovel.domain.enums import ResourceState
from shovel.exceptions.resource import InvalidResourceStateTransitionError

#: 允许的状态转移。key 是当前状态, value 是可以转移过去的状态集合。
#:
#: 设计上的几个关键取舍:
#:
#: * 任何非终态都可以进 ``REVOKED`` —— 用户随时有权吊销一个资源, 不该
#:   被"必须先暂停"之类的流程挡住。
#: * ``VERIFYING`` 可以退回 ``DRAFT`` —— 校验过程本身崩溃(不是校验失败,
#:   而是进程被杀)时, 需要一条路把资源从"卡在 verifying"里捞回来。
#: * ``PAUSED`` 不能直接进 ``DEGRADED`` —— 暂停期间根本没在跑, 无从得知
#:   它是否降级。想知道就得先 ``VERIFYING``。
#: * ``ACTIVE`` 与 ``DEGRADED`` 可以互转 —— 这是扫描过程中最频繁的一对
#:   转移(连上了/连不上了), 不该强制绕道 ``VERIFYING``。
#: * 任何非终态都可以退回 ``DRAFT`` —— ``update_config`` 改掉连接配置后,
#:   旧的校验结果对新配置不构成任何证据, 必须回到"未经校验"的起点。
_TRANSITIONS: Final[MappingProxyType[ResourceState, frozenset[ResourceState]]] = (
    MappingProxyType({
        ResourceState.DRAFT: frozenset({
            ResourceState.VERIFYING,
            ResourceState.REVOKED,
        }),
        ResourceState.VERIFYING: frozenset({
            ResourceState.ACTIVE,
            ResourceState.DEGRADED,
            ResourceState.DRAFT,
            ResourceState.REVOKED,
        }),
        ResourceState.ACTIVE: frozenset({
            ResourceState.DEGRADED,
            ResourceState.PAUSED,
            ResourceState.VERIFYING,
            ResourceState.DRAFT,
            ResourceState.REVOKED,
        }),
        ResourceState.DEGRADED: frozenset({
            ResourceState.ACTIVE,
            ResourceState.PAUSED,
            ResourceState.VERIFYING,
            ResourceState.DRAFT,
            ResourceState.REVOKED,
        }),
        ResourceState.PAUSED: frozenset({
            ResourceState.ACTIVE,
            ResourceState.VERIFYING,
            ResourceState.DRAFT,
            ResourceState.REVOKED,
        }),
        # 终态: 吊销是单向门。想再用, 建一个新资源。
        ResourceState.REVOKED: frozenset(),
    })
)


#: 允许被扫描的状态。必须与 ``Resource.is_scannable`` 保持一致 ——
#: 两处若不一致, 调度器和 UI 会对同一个资源给出相反的答案。
SCANNABLE_STATES: Final[frozenset[ResourceState]] = frozenset({
    ResourceState.ACTIVE,
    ResourceState.DEGRADED,
})

#: 终态集合。语义上等价于 "转移表里出边为空", 这里显式写出来是为了让
#: 调用方能直接问 ``is_terminal()`` 而不必自己去数出边。
TERMINAL_STATES: Final[frozenset[ResourceState]] = frozenset({
    ResourceState.REVOKED,
})


def allowed_targets(state: ResourceState) -> frozenset[ResourceState]:
    """``state`` 可以转移到哪些状态。"""

    return _TRANSITIONS[state]


def can_transition(old: ResourceState, new: ResourceState) -> bool:
    return new in _TRANSITIONS[old]


def is_terminal(state: ResourceState) -> bool:
    return state in TERMINAL_STATES


def is_scannable(state: ResourceState) -> bool:
    return state in SCANNABLE_STATES


def assert_transition(
    old: ResourceState,
    new: ResourceState,
    *,
    resource_ref: str,
) -> None:
    """非法转移直接抛异常。

    ``old == new`` 也视为非法。允许自转移看上去无害, 实际上会掩盖
    "我以为我改了状态, 其实没改"这类 bug —— 幂等应该由调用方显式判断,
    而不是由状态机默默吞掉。
    """

    if old == new:
        raise InvalidResourceStateTransitionError(
            resource_ref,
            old.value,
            new.value,
            _values(allowed_targets(old)),
        )

    if new not in _TRANSITIONS[old]:
        raise InvalidResourceStateTransitionError(
            resource_ref,
            old.value,
            new.value,
            _values(allowed_targets(old)),
        )


@dataclass(frozen=True, slots=True)
class StateChange:
    """一次状态转移的结果。

    做成返回值而不是直接改对象, 是为了让 service 层能在同一个事务里
    决定"改还是不改" —— 例如 verify 的结果是 ACTIVE 而当前已经是 ACTIVE 时,
    需要的是"只刷新 last_verified_at", 而不是一次非法的自转移。
    """

    old: ResourceState
    new: ResourceState
    reason: str

    @property
    def is_noop(self) -> bool:
        return self.old == self.new


def plan_transition(
    old: ResourceState,
    new: ResourceState,
    *,
    reason: str,
    resource_ref: str,
) -> StateChange:
    """校验并描述一次转移; 自转移返回 noop 而不报错。

    与 :func:`assert_transition` 的分工: ``assert_transition`` 用在
    "调用方明确要求状态必须变"的路径 (pause / resume / revoke),
    ``plan_transition`` 用在"结果可能与现状相同"的路径 (verify)。
    """

    if old == new:
        return StateChange(old=old, new=new, reason=reason)

    assert_transition(old, new, resource_ref=resource_ref)

    return StateChange(old=old, new=new, reason=reason)


def _values(states: Iterable[ResourceState]) -> list[str]:
    return [s.value for s in states]


def _assert_transitions_complete() -> None:
    """导入即自检: 每个 ResourceState 都必须在转移表里有一行。

    与 ``enums._assert_layers_complete`` 同样的思路 —— 新增一个状态却忘了
    写转移规则, 应该在 import 时就炸, 而不是等到某个用户恰好走到那条路径时
    才抛 KeyError。
    """

    missing = set(ResourceState) - set(_TRANSITIONS)
    if missing:
        raise RuntimeError(
            "ResourceState members missing a transition rule: "
            f"{sorted(m.value for m in missing)}"
        )

    # 转移目标也必须是合法状态, 且不能指向自己
    for source, targets in _TRANSITIONS.items():
        if source in targets:
            raise RuntimeError(
                f"ResourceState '{source.value}' 的转移表里包含自己, "
                "自转移不是合法转移。"
            )


_assert_transitions_complete()
