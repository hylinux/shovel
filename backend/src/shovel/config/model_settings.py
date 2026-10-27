# ---------------------------------------------------------------------
# 默认大模型配置
#
# 日期: 2026-09-15
# 作者: HongWei Guo <hongweig@163.com>
# ---------------------------------------------------------------------
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ModelProviderType(StrEnum):
    """大模型供应商。

    取值是字符串而不是自增整数: 配置文件既要给人读, 也要能写回磁盘。
    整数枚举既没法让用户看懂 ``model_provider_type = 5`` 指的是谁,
    也会在新增成员时把已有配置文件的语义悄悄改掉。
    """

    #: 未配置。显式写进配置文件, 用户才知道"这里该填点什么"。
    UNSET = ""

    OPENAI = "openai"
    AZUREOPENAI = "azure_openai"
    AZUREFOUNDRY = "azure_foundry"
    MICRSOFTCOPILOT = "microsoft_copilot"
    QIANWEN = "qianwen"
    DEEPSEEK = "deepseek"
    KIMI = "kimi"
    OLLAMA = "ollama"


class DefaultAgentModelSettings(BaseModel):
    """``[model]`` 段: 默认对话大模型。

    所有字段默认都是空串而不是 ``None``: TOML 没有 null, ``None`` 只能
    靠"省略该键"表达, 结果是 ``shovel init`` 生成出一个空的 ``[model]``
    表 —— 用户看不到任何可填的键名, 只能去翻源码。
    """

    model_provider_type: ModelProviderType = ModelProviderType.UNSET
    base_endpoint: str = ""
    model_name: str = ""
    api_version: str = ""
    security_key: str = ""
    max_input_chars: int = Field(default=24000, ge=2000, le=500000)
    max_output_tokens: int = Field(default=2048, ge=1, le=32768)
    timeout_seconds: float = Field(default=120, ge=5, le=600)

    @property
    def is_configured(self) -> bool:
        return bool(self.model_provider_type and self.model_name)
