#---------------------------------------------------------
# 知识库 embedding 配置
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""``[embedding]`` 段: 知识库用哪个模型做向量化。

为什么它不复用 ``[memory.embedder]``: 那是记忆子系统的 embedding, 由
mem0 托管、写进 Qdrant、维度由 ``memory.dim`` 决定。知识库的 embedding
写进 Zvec、维度由 ``zvec.dim`` 决定。两套向量互不相通, 用同一份配置
只会让"改一个维度炸掉另一个库"成为常态。

同理也不回落到 ``[model]`` 的对话模型: 拿 gpt-4 的名字去请求
``/v1/embeddings`` 必然失败, 而失败会发生在扫描跑了十分钟之后。
宁可在启动时就说"你还没配 embedding"。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr, model_validator


class EmbeddingSettings(BaseModel):
    """知识库向量化配置。

    provider 取值与 mem0 的命名保持一致 (openai / azure_openai / ollama),
    这样用户在两个配置段之间不需要记两套词汇。
    """

    #: 留空表示"未配置"。此时带 embed 的扫描会明确报错而不是静默跳过 ——
    #: 静默跳过的结果是一个建好了却搜不出东西的知识库。
    provider: str = ""

    model: str = ""

    #: OpenAI 兼容端点的根地址, 例如 ``http://127.0.0.1:11434/v1``。
    #: Azure 则填 resource endpoint。
    base_url: str = ""

    #: 留空表示端点不需要鉴权(本地 ollama / vLLM 就是这种情况)。
    #: 用空 SecretStr 而不是 None: None 无法写进 TOML, 这个键会在
    #: 生成的配置文件里整个消失, 用户根本不知道该往哪儿填密钥。
    api_key: SecretStr = SecretStr("")

    #: 仅 Azure 需要。
    api_version: str = ""

    #: 一次请求塞多少条文本。
    #:
    #: 32 是个保守值: 再大能省往返, 但一旦超过服务端的单请求 token 上限,
    #: 整批都会失败, 而重试要从头再来。本地模型 (ollama) 的并发能力通常
    #: 更弱, 批量过大反而会超时。
    batch_size: int = Field(default=32, ge=1, le=512)

    #: 单次 HTTP 请求超时 (秒)。本地模型第一次加载权重可能要几十秒,
    #: 所以默认值比一般 API 调用宽松得多。
    timeout_seconds: float = Field(default=120.0, gt=0)

    #: 失败重试次数。只对可重试错误 (超时、5xx、429) 生效;
    #: 401/400 这类重试一万次也不会变好的错误会立刻放弃。
    max_retries: int = Field(default=3, ge=0, le=10)

    #: 送进模型的单条文本字符上限。超出部分截断。
    #:
    #: 必要性: 切块器按字符预算工作, 而字符到 token 的折算是估的。
    #: 一段全是罕见字符的文本可能超出模型上限, 服务端会拒整批。
    #: 这道闸门保证"最坏情况下是一条被截短的文本", 而不是"一批全废"。
    max_input_chars: int = Field(default=8000, ge=100)

    @property
    def is_configured(self) -> bool:
        return bool(self.provider and self.model)

    @model_validator(mode="after")
    def _check_azure(self) -> EmbeddingSettings:
        if self.provider == "azure_openai" and self.provider and not self.api_version:
            raise ValueError(
                "provider=azure_openai 时必须设置 embedding.api_version, "
                "否则请求会被 Azure 直接拒绝。"
            )

        return self
