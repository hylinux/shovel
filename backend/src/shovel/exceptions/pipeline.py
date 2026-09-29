#---------------------------------------------------------
# 扫描流水线的异常定义
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#
# 这里的异常分成两类, 区分它们比给每一个都起个名字更重要:
#
#   * **单篇文档级** (ParseError 及其子类) —— 一个文件解析不了, 不该让
#     另外 9999 个文件陪葬。流水线捕获它, 记进 job_event, 把这一篇标成
#     FAILED, 然后继续。
#   * **作业级** (EmbeddingError / VectorSinkError) —— embedding 端点
#     连不上, 后面每一篇都会以同样的方式失败。继续跑只是在把同一条
#     错误刷 9999 遍, 所以它们会中止整个 Job。
#
# 这条界线直接体现在 scan_service 的 except 子句里。
#---------------------------------------------------------
from __future__ import annotations

from shovel.exceptions.base import ShovelError
from shovel.exceptions.exit_codes import ExitCodes


class PipelineError(ShovelError):
    """扫描流水线所有异常的根。"""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int = ExitCodes.GENERAL_ERROR,
        hint: str | None = None,
        details: str | None = None,
    ) -> None:
        super().__init__(
            message,
            exit_code=exit_code,
            hint=hint,
            details=details,
        )


# --------------------------------------------------------------------- #
# 单篇文档级
# --------------------------------------------------------------------- #
class ParseError(PipelineError):
    """解析一篇文档失败。

    带 ``code`` 是为了让它能原样落进 ``job_event.code``: 用户排查
    "为什么这 30 个文件没进来"时, 按 code 分组比读 30 条自然语言
    消息有用得多。
    """

    code: str = "PARSE_FAILED"

    def __init__(self, uri: str, reason: str, *, hint: str | None = None) -> None:
        self.uri = uri
        self.reason = reason
        super().__init__(
            f"解析失败: {uri}",
            exit_code=ExitCodes.GENERAL_ERROR,
            hint=hint,
            details=reason,
        )


class UnsupportedDocumentError(ParseError):
    """没有任何解析器认领这种文档。

    这**不是** bug: 用户的目录里本来就会有 .zip、.exe、.dll。
    流水线把它们标成 SKIPPED 而不是 FAILED —— 失败计数应该只反映
    "本该处理却没处理成"的那些。
    """

    code = "PARSE_UNSUPPORTED"

    def __init__(self, uri: str, suffix: str) -> None:
        super().__init__(
            uri,
            f"没有能处理 '{suffix or '(无扩展名)'}' 的解析器。",
            hint="当前支持: txt/log/md/pdf/docx/xlsx/pptx 及纯文本代码文件。",
        )


class EncryptedDocumentError(ParseError):
    """文档有密码。

    单独成类是因为它的处置方式不同: 重试永远不会成功, 只有用户提供
    密码或解密原件才行, 所以流水线不该把它排进重试队列。
    """

    code = "PARSE_ENCRYPTED"

    def __init__(self, uri: str) -> None:
        super().__init__(
            uri,
            "文档被加密, 需要密码才能打开。",
            hint="请先解除文档密码, 或把它排除在扫描范围之外。",
        )


class EmptyDocumentError(ParseError):
    """解析成功, 但一个字都没提取出来。

    最常见的成因是扫描件 PDF —— 里面只有图片, 没有文本层。
    把它和"解析器崩了"区分开, 用户才知道该去装 OCR 而不是来报 bug。
    """

    code = "PARSE_EMPTY"

    def __init__(self, uri: str) -> None:
        super().__init__(
            uri,
            "没有提取到任何文本。",
            hint="若是扫描件 PDF, 它只有图像层而没有文本层, 需要 OCR 才能索引。",
        )


# --------------------------------------------------------------------- #
# 作业级
# --------------------------------------------------------------------- #
class EmbeddingError(PipelineError):
    """调用 embedding 服务失败。"""

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(
            f"embedding 服务调用失败 ({provider})。",
            exit_code=ExitCodes.NETWORK_ERROR,
            hint="检查 settings.toml 的 [embedding] 端点与密钥, 或确认本地模型服务已启动。",
            details=reason,
        )


class EmbeddingDimensionMismatchError(PipelineError):
    """模型返回的维度与向量库 collection 的维度不一致。

    必须硬失败。维度不匹配时写进去的向量不是"差一点", 而是完全无法
    参与检索 —— 而这个错误要等到用户搜索时才会暴露, 那时已经烧掉了
    一整轮 embedding 的时间和费用。
    """

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            f"embedding 维度不匹配: collection 要求 {expected}, 模型返回 {actual}。",
            exit_code=ExitCodes.CONFIG_ERROR,
            hint=(
                f"把 settings.toml 的 zvec.dim 改成 {actual} 并重建 collection, "
                "或换回原来的 embedding 模型。换模型 = 换维度 = 必须重建。"
            ),
        )


class EmbeddingNotConfiguredError(PipelineError):
    """没有配 embedding 服务就想跑带 embed 的扫描。"""

    def __init__(self) -> None:
        super().__init__(
            "尚未配置 embedding 服务。",
            exit_code=ExitCodes.CONFIG_ERROR,
            hint=(
                "在 settings.toml 里补上 [embedding] 的 provider / model / base_url, "
                "或者加 --no-embed 先只做解析与切块 (向量留到之后补跑)。"
            ),
        )


class VectorSinkError(PipelineError):
    """向量写入失败。"""

    def __init__(self, reason: str) -> None:
        super().__init__(
            "向量写入失败。",
            exit_code=ExitCodes.GENERAL_ERROR,
            hint="执行 `shovel init` 确认 collection 已建好, 再重试。",
            details=reason,
        )


class JobNotFoundError(PipelineError):

    def __init__(self, job_id: str) -> None:
        super().__init__(
            f"作业 '{job_id}' 不存在。",
            exit_code=ExitCodes.CONFIG_ERROR,
            hint="用 `shovel scan list` 查看历史作业。",
        )


class ResourceNotScannableError(PipelineError):
    """资源状态不允许扫描。

    只有 ACTIVE / DEGRADED 能扫: DRAFT 还没验证过连通性, PAUSED 是
    用户明确要求停下, REVOKED 连凭据都销毁了。
    """

    def __init__(self, ref: str, state: str) -> None:
        super().__init__(
            f"资源 '{ref}' 当前状态是 '{state}', 不能扫描。",
            exit_code=ExitCodes.CONFIG_ERROR,
            hint="先运行 `shovel resource verify` 让它回到 active, 或 `shovel resource resume` 恢复。",
        )


class ScanModeUnsupportedError(PipelineError):
    """连接器不具备该扫描模式所需的能力。"""

    def __init__(self, kind: str, mode: str) -> None:
        super().__init__(
            f"连接器 '{kind}' 不支持 '{mode}' 扫描模式。",
            exit_code=ExitCodes.CONFIG_ERROR,
            hint="用 `shovel resource connectors` 查看该连接器声明了哪些能力。",
        )
