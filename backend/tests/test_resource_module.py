"""资源模块: 注册表、状态机与凭据层的测试。"""

from __future__ import annotations

import os

import pytest
from pydantic import BaseModel, SecretStr

from shovel.domain.enums import ResourceState, ScanMode, Sensitivity
from shovel.domain.resource_state import (
    SCANNABLE_STATES,
    allowed_targets,
    assert_transition,
    can_transition,
    is_terminal,
    plan_transition,
)
from shovel.exceptions.resource import (
    CredentialNotFoundError,
    DuplicateConnectorError,
    InvalidCredentialRefError,
    InvalidResourceStateTransitionError,
    UnknownConnectorError,
)
from shovel.services import connector_registry
from shovel.services.connector_base import (
    ConnectorCapability,
    ConnectorSpec,
    VerifyResult,
)
from shovel.services.credentials import (
    CredentialRef,
    CredentialScheme,
    EnvCredentialStore,
    InMemoryCredentialStore,
)

# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #


class _DummyConfig(BaseModel):
    pass


def _spec(kind: str, **kw: object) -> ConnectorSpec:
    return ConnectorSpec(
        kind=kind,
        display_name=kind,
        description="测试用",
        config_model=_DummyConfig,
        capabilities=frozenset({ConnectorCapability.DISCOVER}),
        **kw,  # type: ignore[arg-type]
    )


def test_builtin_local_fs_is_registered() -> None:
    assert "local_fs" in connector_registry.kinds()
    assert connector_registry.get_spec("local_fs").kind == "local_fs"


def test_unknown_kind_lists_available_ones() -> None:
    """报错信息必须把合法取值列出来 —— 否则用户没办法确认自己拼错了没有。"""

    with pytest.raises(UnknownConnectorError) as excinfo:
        connector_registry.get("notion")

    assert "local_fs" in (excinfo.value.hint or "")


def test_duplicate_kind_is_rejected() -> None:
    """重复注册必须报错。后者覆盖前者会让生效的实现取决于 import 顺序。"""

    class First:
        spec = _spec("dup_kind")

    class Second:
        spec = _spec("dup_kind")

    connector_registry._reset_for_tests()
    try:
        connector_registry.register(First)
        with pytest.raises(DuplicateConnectorError):
            connector_registry.register(Second)
    finally:
        connector_registry._reset_for_tests(load_builtins=True)


def test_registering_same_class_twice_is_idempotent() -> None:
    class Once:
        spec = _spec("idem_kind")

    connector_registry._reset_for_tests()
    try:
        connector_registry.register(Once)
        connector_registry.register(Once)  # 同一个类, 不该报错
        assert connector_registry.has("idem_kind")
    finally:
        connector_registry._reset_for_tests(load_builtins=True)


def test_class_without_spec_cannot_register() -> None:
    class NotAConnector:
        pass

    with pytest.raises(TypeError):
        connector_registry.register(NotAConnector)


def test_spec_requires_discover_capability() -> None:
    """列举不出任何东西的数据源没有存在意义。"""

    with pytest.raises(ValueError, match="DISCOVER"):
        ConnectorSpec(
            kind="no_discover",
            display_name="x",
            description="x",
            config_model=_DummyConfig,
            capabilities=frozenset({ConnectorCapability.RANDOM_READ}),
        )


def test_spec_rejects_out_of_range_authority() -> None:
    with pytest.raises(ValueError, match="default_authority"):
        _spec("bad_authority", default_authority=1.5)


def test_capabilities_gate_scan_modes() -> None:
    """没有 INCREMENTAL 能力就只能全量扫。"""

    only_discover = _spec("discover_only")

    assert only_discover.supports_mode(ScanMode.FULL_SWEEP)
    assert only_discover.supports_mode(ScanMode.TARGETED)
    assert not only_discover.supports_mode(ScanMode.INCREMENTAL)
    assert only_discover.default_scan_mode() is ScanMode.FULL_SWEEP

    local_fs = connector_registry.get_spec("local_fs")
    assert local_fs.supports_mode(ScanMode.INCREMENTAL)
    # 能增量就增量: 全量扫描在用户的工作机上是最贵的动作
    assert local_fs.default_scan_mode() is ScanMode.INCREMENTAL


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #
def test_every_state_has_a_transition_rule() -> None:
    for state in ResourceState:
        assert isinstance(allowed_targets(state), frozenset)


def test_revoked_is_terminal() -> None:
    assert is_terminal(ResourceState.REVOKED)
    assert allowed_targets(ResourceState.REVOKED) == frozenset()

    for state in ResourceState:
        if state is ResourceState.REVOKED:
            continue
        assert not can_transition(ResourceState.REVOKED, state)


def test_anything_can_be_revoked() -> None:
    """用户随时有权吊销一个资源, 不该被流程挡住。"""

    for state in ResourceState:
        if state is ResourceState.REVOKED:
            continue
        assert can_transition(state, ResourceState.REVOKED)


def test_draft_cannot_jump_straight_to_active() -> None:
    """没被校验过就显示 ACTIVE, 会让调度器排进一个连不上的资源。"""

    assert not can_transition(ResourceState.DRAFT, ResourceState.ACTIVE)

    with pytest.raises(InvalidResourceStateTransitionError):
        assert_transition(
            ResourceState.DRAFT, ResourceState.ACTIVE, resource_ref="r",
        )


def test_paused_cannot_go_degraded_directly() -> None:
    """暂停期间没在跑, 无从得知它是否降级。"""

    assert not can_transition(ResourceState.PAUSED, ResourceState.DEGRADED)


def test_active_and_degraded_flip_freely() -> None:
    """扫描过程中最频繁的一对转移, 不该强制绕道 VERIFYING。"""

    assert can_transition(ResourceState.ACTIVE, ResourceState.DEGRADED)
    assert can_transition(ResourceState.DEGRADED, ResourceState.ACTIVE)


def test_self_transition_is_rejected_by_assert() -> None:
    """允许自转移会掩盖 '我以为我改了状态, 其实没改' 这类 bug。"""

    with pytest.raises(InvalidResourceStateTransitionError):
        assert_transition(
            ResourceState.ACTIVE, ResourceState.ACTIVE, resource_ref="r",
        )


def test_plan_transition_treats_self_transition_as_noop() -> None:
    """verify 的结果可能与现状相同, 那是 noop 而不是错误。"""

    change = plan_transition(
        ResourceState.ACTIVE,
        ResourceState.ACTIVE,
        reason="仍然正常",
        resource_ref="r",
    )

    assert change.is_noop
    assert change.new is ResourceState.ACTIVE


def test_scannable_states_match_the_orm_property() -> None:
    """与 Resource.is_scannable 不一致的话, 调度器和 UI 会给出相反的答案。"""

    from shovel.db.model.config import Resource

    for state in ResourceState:
        resource = Resource(name="x", connector_kind="local_fs", state=state)
        assert resource.is_scannable == (state in SCANNABLE_STATES)


# --------------------------------------------------------------------------- #
# 凭据
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "scheme", "account"),
    [
        ("keyring:res_abc", CredentialScheme.KEYRING, "res_abc"),
        ("env:MY_TOKEN", CredentialScheme.ENV, "MY_TOKEN"),
        ("  env:MY_TOKEN  ", CredentialScheme.ENV, "MY_TOKEN"),
        ("ENV:MY_TOKEN", CredentialScheme.ENV, "MY_TOKEN"),
    ],
)
def test_credential_ref_parses(raw: str, scheme: CredentialScheme, account: str) -> None:
    ref = CredentialRef.parse(raw)
    assert ref.scheme is scheme
    assert ref.account == account


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "no_scheme",
        "keyring:",
        "vault:secret",
        # shell 注入式的变量名必须在解析阶段就被拒绝
        "env:FOO; rm -rf /",
        "env:1BAD",
    ],
)
def test_credential_ref_rejects_garbage(raw: str) -> None:
    with pytest.raises(InvalidCredentialRefError):
        CredentialRef.parse(raw)


def test_env_refs_are_read_only() -> None:
    """进程改不了用户的 shell, 假装能写只会误导人。"""

    ref = CredentialRef.parse("env:MY_TOKEN")
    assert not ref.is_writable

    store = EnvCredentialStore()
    with pytest.raises(InvalidCredentialRefError):
        store.set(ref, SecretStr("x"))


def test_keyring_refs_are_writable() -> None:
    assert CredentialRef.for_resource("res_1").is_writable


def test_env_store_reads_from_environment() -> None:
    ref = CredentialRef.parse("env:SHOVEL_TEST_TOKEN")
    store = EnvCredentialStore()

    os.environ["SHOVEL_TEST_TOKEN"] = "s3cret"
    try:
        assert store.get(ref).get_secret_value() == "s3cret"
    finally:
        del os.environ["SHOVEL_TEST_TOKEN"]


def test_env_store_treats_empty_as_missing() -> None:
    """一个空 token 不可能是用户的本意, 早报错比走到 HTTP 401 便宜。"""

    ref = CredentialRef.parse("env:SHOVEL_TEST_EMPTY")
    store = EnvCredentialStore()

    os.environ["SHOVEL_TEST_EMPTY"] = ""
    try:
        with pytest.raises(CredentialNotFoundError):
            store.get(ref)
    finally:
        del os.environ["SHOVEL_TEST_EMPTY"]


def test_in_memory_store_repr_hides_secrets() -> None:
    """密钥绝不该出现在 repr 里 —— 日志和栈回溯都会打印它。"""

    store = InMemoryCredentialStore()
    ref = CredentialRef.for_resource("res_1")
    store.set(ref, SecretStr("super-secret-value"))

    assert "super-secret-value" not in repr(store)
    assert store.get(ref).get_secret_value() == "super-secret-value"


def test_secret_str_hides_value_in_repr() -> None:
    assert "super-secret" not in repr(SecretStr("super-secret"))


def test_deleting_a_missing_credential_is_not_an_error() -> None:
    """删除资源这条路径不该因为密钥早被手动清掉了而失败。"""

    store = InMemoryCredentialStore()
    store.delete(CredentialRef.for_resource("never_existed"))


# --------------------------------------------------------------------------- #
# VerifyResult
# --------------------------------------------------------------------------- #
def test_verify_failure_is_a_value_not_an_exception() -> None:
    """连不上是资源的常态, 常态不该用异常表达。"""

    result = VerifyResult.failure("目录不存在", details={"root_path": "/nope"})

    assert result.ok is False
    assert result.details["root_path"] == "/nope"


def test_verify_details_default_is_not_shared() -> None:
    a = VerifyResult.success("ok")
    b = VerifyResult.success("ok")
    assert a.details is not b.details


def test_sensitivity_rank_is_ordered() -> None:
    assert Sensitivity.NORMAL.rank < Sensitivity.PERSONAL.rank < Sensitivity.SECRET.rank
