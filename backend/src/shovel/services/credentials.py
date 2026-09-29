#---------------------------------------------------------
# 凭据存储
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""资源的密钥怎么存。

## 核心约定: 数据库里只有引用, 没有秘密

``Resource.identity_ref`` 存的是一个**引用字符串**, 形如:

```text
keyring:res_3f2a...      -> 去操作系统凭据库里按这个 account 取
env:NOTION_TOKEN         -> 去环境变量里取
```

数据库文件会被备份、会被同步、会被用户直接用 sqlite3 打开看。任何写进
``config_json`` 的密钥, 都等于写进了这些地方。把秘密留在 keyring 或环境
变量里, 泄露面就只剩"进程内存"这一处。

## 为什么 keyring 不可用时必须硬失败

最容易写、也最糟糕的实现是"keyring 拿不到就退回明文文件"。用户会看到
"凭据已保存"、以为东西在系统凭据库里, 而实际上它躺在一个 644 的文件里。
一个安全机制最坏的形态不是缺席, 而是看起来在、其实不在。

所以这里的选择是: 后端不可用就抛 :class:`CredentialBackendUnavailableError`,
并在 hint 里告诉用户改用 ``env:`` 引用 —— 那是一个用户**知道**其安全边界
的方案。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from pydantic import SecretStr

from shovel.exceptions.resource import (
    CredentialBackendUnavailableError,
    CredentialNotFoundError,
    InvalidCredentialRefError,
)

#: keyring 里用的 service 名。所有 Shovel 的凭据都挂在这一个 service 下,
#: 用 account 区分, 这样用户在系统凭据管理器里能一眼看到"哪些是 Shovel 的"。
KEYRING_SERVICE: Final[str] = "shovel"

#: 环境变量名的合法字符。限制它是为了让 "env:FOO; rm -rf /" 这种东西
#: 在解析阶段就被拒绝, 而不是变成一次 os.environ 的诡异 KeyError。
_ENV_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CredentialScheme(StrEnum):
    KEYRING = "keyring"
    ENV = "env"


@dataclass(frozen=True, slots=True)
class CredentialRef:
    """一个凭据引用, 形如 ``keyring:res_xxx`` 或 ``env:MY_TOKEN``。

    做成值对象而不是到处传裸字符串, 是因为"这个字符串是不是合法引用"
    这个判断只应该存在一处。裸字符串会让每个使用点都得自己 split 一次,
    而其中总有一处会忘了处理没有冒号的情况。
    """

    scheme: CredentialScheme
    account: str

    def __str__(self) -> str:
        return f"{self.scheme.value}:{self.account}"

    @property
    def is_writable(self) -> bool:
        """能不能写。环境变量是只读的 —— 进程改不了用户的 shell。"""

        return self.scheme is CredentialScheme.KEYRING

    @classmethod
    def parse(cls, raw: str) -> CredentialRef:
        text = (raw or "").strip()

        if ":" not in text:
            raise InvalidCredentialRefError(raw)

        scheme_text, _, account = text.partition(":")
        account = account.strip()

        if not account:
            raise InvalidCredentialRefError(raw)

        try:
            scheme = CredentialScheme(scheme_text.strip().lower())
        except ValueError as exc:
            raise InvalidCredentialRefError(raw) from exc

        if scheme is CredentialScheme.ENV and not _ENV_NAME_RE.match(account):
            raise InvalidCredentialRefError(raw)

        return cls(scheme=scheme, account=account)

    @classmethod
    def for_resource(cls, resource_id: str) -> CredentialRef:
        """某个资源默认的 keyring 引用。

        用 ``resource_id`` 而不是资源名做 account: 名字可以被改,
        而改名不该让已经存好的密钥失联。
        """

        return cls(scheme=CredentialScheme.KEYRING, account=resource_id)


class CredentialStore(Protocol):
    """凭据读写的抽象。

    做成 Protocol 是为了测试: ``ResourceService`` 的绝大多数测试不该
    依赖本机有没有 keyring 后端, 塞一个内存实现进去就行。
    """

    def get(self, ref: CredentialRef) -> SecretStr: ...

    def set(self, ref: CredentialRef, secret: SecretStr) -> None: ...

    def delete(self, ref: CredentialRef) -> None: ...

    def supports(self, ref: CredentialRef) -> bool: ...


class EnvCredentialStore:
    """从环境变量读。只读。

    存在的意义有两个: 一是 CI 和容器里根本没有 keyring 后端, 二是不少
    用户本来就把 token 放在 ``.envrc`` / systemd unit 里, 让他们再往
    keyring 抄一份没有收益。
    """

    def supports(self, ref: CredentialRef) -> bool:
        return ref.scheme is CredentialScheme.ENV

    def get(self, ref: CredentialRef) -> SecretStr:
        value = os.environ.get(ref.account)

        # 空字符串按"没设置"处理: 一个空的 token 不可能是用户的本意,
        # 而让它一路走到 HTTP 401 才报错, 排查成本要高得多。
        if not value:
            raise CredentialNotFoundError(str(ref))

        return SecretStr(value)

    def set(self, ref: CredentialRef, secret: SecretStr) -> None:
        raise InvalidCredentialRefError(str(ref))

    def delete(self, ref: CredentialRef) -> None:
        raise InvalidCredentialRefError(str(ref))


class KeyringCredentialStore:
    """存进操作系统凭据库。

    ``keyring`` 是惰性 import 的: 它在部分 Linux 发行版上会拖起 dbus,
    而一个只用 ``env:`` 引用的用户不该为此付出启动开销, 更不该因为
    dbus 不在就连 ``shovel version`` 都跑不起来。
    """

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self._service = service

    def supports(self, ref: CredentialRef) -> bool:
        return ref.scheme is CredentialScheme.KEYRING

    def get(self, ref: CredentialRef) -> SecretStr:
        backend = self._backend()

        try:
            value = backend.get_password(self._service, ref.account)
        except Exception as exc:
            raise CredentialBackendUnavailableError(str(exc)) from exc

        if not value:
            raise CredentialNotFoundError(str(ref))

        return SecretStr(value)

    def set(self, ref: CredentialRef, secret: SecretStr) -> None:
        backend = self._backend()

        try:
            backend.set_password(
                self._service,
                ref.account,
                secret.get_secret_value(),
            )
        except Exception as exc:
            raise CredentialBackendUnavailableError(str(exc)) from exc

    def delete(self, ref: CredentialRef) -> None:
        backend = self._backend()

        try:
            backend.delete_password(self._service, ref.account)
        except Exception:
            # 删一个不存在的凭据不是错误。delete 的调用方通常是
            # "删除资源"这条路径, 它不该因为密钥早就被手动清掉了而失败。
            return

    @staticmethod
    def _backend() -> Any:
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring
        except ImportError as exc:
            raise CredentialBackendUnavailableError(
                "未安装 keyring 包。"
            ) from exc

        backend = keyring.get_keyring()

        # keyring 在找不到任何可用后端时不会报错, 而是装上一个
        # 调用即抛异常的 FailKeyring。必须显式识别它, 否则错误信息
        # 会变成一句用户看不懂的 NoKeyringError。
        if isinstance(backend, FailKeyring):
            raise CredentialBackendUnavailableError(
                "keyring 没有找到任何可用的系统后端。"
            )

        return backend


class ChainedCredentialStore:
    """按 ref 的 scheme 分发给对应的实现。

    这是给 service 层用的默认 store —— 它不必关心某个资源用的是
    keyring 还是环境变量, 引用字符串自己带着答案。
    """

    def __init__(self, *stores: CredentialStore) -> None:
        if not stores:
            raise ValueError("ChainedCredentialStore 至少需要一个后端。")
        self._stores = stores

    def supports(self, ref: CredentialRef) -> bool:
        return any(store.supports(ref) for store in self._stores)

    def get(self, ref: CredentialRef) -> SecretStr:
        return self._pick(ref).get(ref)

    def set(self, ref: CredentialRef, secret: SecretStr) -> None:
        self._pick(ref).set(ref, secret)

    def delete(self, ref: CredentialRef) -> None:
        self._pick(ref).delete(ref)

    def _pick(self, ref: CredentialRef) -> CredentialStore:
        for store in self._stores:
            if store.supports(ref):
                return store
        raise InvalidCredentialRefError(str(ref))


class InMemoryCredentialStore:
    """纯内存实现, 给测试和 dry-run 用。

    放在生产代码里而不是 tests/ 下, 是因为 ``ResourceService`` 的
    "不落盘预演"模式也需要它 —— 一个只在测试里存在的实现,
    迟早会和真实接口漂移。
    """

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def supports(self, ref: CredentialRef) -> bool:
        return True

    def get(self, ref: CredentialRef) -> SecretStr:
        value = self._values.get(str(ref))
        if not value:
            raise CredentialNotFoundError(str(ref))
        return SecretStr(value)

    def set(self, ref: CredentialRef, secret: SecretStr) -> None:
        self._values[str(ref)] = secret.get_secret_value()

    def delete(self, ref: CredentialRef) -> None:
        self._values.pop(str(ref), None)

    def __repr__(self) -> str:
        # 绝不打印值。只说有几条。
        return f"<InMemoryCredentialStore entries={len(self._values)}>"


def build_credential_store() -> CredentialStore:
    """默认的 store: keyring 优先, env 兜底。

    顺序不影响正确性(分发看的是 scheme 而不是顺序), 但它决定了
    ``supports()`` 的短路顺序, 保持 keyring 在前更符合直觉。
    """

    return ChainedCredentialStore(
        KeyringCredentialStore(),
        EnvCredentialStore(),
    )
