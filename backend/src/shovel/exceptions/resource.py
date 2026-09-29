#---------------------------------------------------------
# 资源模块的异常定义
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#
# 每个异常都带 hint: CliExceptionHandler 会把它单独渲染出来。
# 一条只说"出了什么事", 却不说"我下一步该做什么"的错误信息,
# 对用户来说等于没说。
#---------------------------------------------------------
from __future__ import annotations

from collections.abc import Iterable

from shovel.exceptions.base import ShovelError
from shovel.exceptions.exit_codes import ExitCodes


class ResourceError(ShovelError):
    """资源模块所有异常的根。

    默认落在 CONFIG_ERROR 上: 资源本质是一份用户配置, 绝大多数失败
    (连接器不存在、配置字段写错、状态不对) 都是配置问题, 而不是程序 bug。
    个别子类会覆盖成更贴切的 exit code。
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: int = ExitCodes.CONFIG_ERROR,
        hint: str | None = None,
        details: str | None = None,
    ) -> None:
        super().__init__(
            message,
            exit_code=exit_code,
            hint=hint,
            details=details,
        )


class ResourceNotFoundError(ResourceError):

    def __init__(self, ref: str) -> None:
        super().__init__(
            f"资源 '{ref}' 不存在。",
            hint="用 `shovel resource list` 查看已有的资源。",
        )


class DuplicateResourceNameError(ResourceError):

    def __init__(self, name: str) -> None:
        super().__init__(
            f"资源名 '{name}' 已被占用。",
            hint="资源名全局唯一。换一个名字, 或用 `shovel resource show` 查看已有的那个。",
        )


class UnknownConnectorError(ResourceError):
    """连接器 kind 不在注册表里。

    错误信息里必须列出所有可用 kind。connector_kind 是一个自由字符串,
    用户唯一能确认自己有没有拼错的方式, 就是看到完整的合法取值。
    """

    def __init__(self, kind: str, available: Iterable[str]) -> None:
        known = ", ".join(sorted(available)) or "(注册表为空)"
        super().__init__(
            f"未知的连接器类型 '{kind}'。",
            hint=f"可用的连接器: {known}。也可以运行 `shovel resource connectors` 查看。",
        )


class DuplicateConnectorError(ResourceError):
    """两个连接器声明了同一个 kind。

    这是开发期错误, 不是用户错误: 注册表在 import 时就会炸,
    而不是等到运行时随机挑一个实现。
    """

    def __init__(self, kind: str, existing: str, incoming: str) -> None:
        super().__init__(
            f"连接器类型 '{kind}' 被重复注册。",
            hint=f"已注册的是 {existing}, 新注册的是 {incoming}。请改掉其中一个的 kind。",
        )


class InvalidConnectorConfigError(ResourceError):
    """config_json 没有通过连接器自己声明的 pydantic 模型。"""

    def __init__(self, kind: str, details: str) -> None:
        super().__init__(
            f"连接器 '{kind}' 的配置不合法。",
            exit_code=ExitCodes.VALIDATION_ERROR,
            hint=f"运行 `shovel resource connectors --kind {kind}` 查看该连接器接受哪些字段。",
            details=details,
        )


class InvalidResourceStateTransitionError(ResourceError):

    def __init__(
        self,
        resource_ref: str,
        old: str,
        new: str,
        allowed: Iterable[str],
    ) -> None:
        allow = ", ".join(sorted(allowed)) or "(终态, 不允许任何转移)"
        super().__init__(
            f"资源 '{resource_ref}' 不能从 '{old}' 转移到 '{new}'。",
            hint=f"从 '{old}' 出发允许的状态: {allow}。",
        )


class CredentialBackendUnavailableError(ResourceError):
    """操作系统没有可用的 keyring 后端。

    这里必须硬失败。静默降级成明文文件是最糟糕的选择:
    用户会以为自己的密钥被安全保存了, 而实际上它躺在一个可读文件里。
    """

    def __init__(self, details: str | None = None) -> None:
        super().__init__(
            "当前系统没有可用的 keyring 后端, 无法安全保存凭据。",
            exit_code=ExitCodes.PERMISSION_ERROR,
            hint=(
                "改用环境变量引用, 例如 --identity-ref env:MY_API_TOKEN; "
                "或者在本机配置 keyring 后端 (Windows 自带 WinVault, "
                "Linux 需要 SecretService 或 KWallet)。"
            ),
            details=details,
        )


class CredentialNotFoundError(ResourceError):

    def __init__(self, ref: str) -> None:
        super().__init__(
            f"凭据引用 '{ref}' 没有对应的值。",
            exit_code=ExitCodes.PERMISSION_ERROR,
            hint=(
                "如果是 env: 引用, 检查该环境变量是否已导出; "
                "如果是 keyring: 引用, 用 `shovel resource set-secret` 重新写入。"
            ),
        )


class InvalidCredentialRefError(ResourceError):

    def __init__(self, ref: str) -> None:
        super().__init__(
            f"无法解析凭据引用 '{ref}'。",
            exit_code=ExitCodes.VALIDATION_ERROR,
            hint="合法格式是 'keyring:<account>' 或 'env:<VAR_NAME>'。",
        )


class ResourceVerificationFailedError(ResourceError):
    """verify() 明确判定失败。

    注意: ``ResourceService.verify()`` 不抛这个异常 —— 它把失败写进
    DEGRADED 状态并正常返回 VerifyResult。只有"调用方要求校验必须通过"
    的场景 (例如扫描启动前的前置检查) 才会抛。
    """

    def __init__(self, ref: str, reason: str) -> None:
        super().__init__(
            f"资源 '{ref}' 校验未通过: {reason}",
            exit_code=ExitCodes.VALIDATION_ERROR,
            hint="修正配置后重新运行 `shovel resource verify`。",
        )


class ConnectorPathEscapeError(ResourceError):
    """条目标识解析出的路径落在了资源根目录之外。

    单独成一类而不是混进 ConnectorCapabilityError: 这不是"能力不够",
    而是一次越界访问 —— 要么是数据被改过, 要么是有人在构造路径,
    两种情况都应该按权限问题处理并留下痕迹。
    """

    def __init__(self, kind: str, external_id: str, root: str) -> None:
        super().__init__(
            f"连接器 '{kind}' 拒绝访问越界路径。",
            exit_code=ExitCodes.PERMISSION_ERROR,
            hint="条目标识必须落在资源根目录之内。若该资源的根目录变过, 请重新执行一次全量扫描。",
            details=f"external_id={external_id!r}, root={root!r}",
        )


class ConnectorCapabilityError(ResourceError):
    """要求连接器做一件它没有声明的事, 例如对不支持 random_read 的连接器调用 open()。"""

    def __init__(self, kind: str, capability: str) -> None:
        super().__init__(
            f"连接器 '{kind}' 不具备 '{capability}' 能力。",
            hint="这是连接器的能力边界, 不是配置问题。换一个连接器, 或者换一种扫描模式。",
        )
