# ---------------------------------------------------------------------
# 默认大模型配置
#
# 日期: 2026-09-15
# 作者: HongWei Guo <hongweig@163.com>
# ---------------------------------------------------------------------
from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, Field, model_validator

from shovel.config.errors import ConfigError


def foundry_v1_endpoint(endpoint: str) -> str:
    try:
        url = urlsplit(endpoint)
        _ = url.port
    except ValueError:
        raise ConfigError("Microsoft Foundry requires a valid HTTP(S) endpoint.") from None
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or any(character.isspace() for character in endpoint)
        or "\\" in endpoint
    ):
        raise ConfigError("Microsoft Foundry requires an HTTP(S) endpoint without credentials or query.")
    path = url.path.rstrip("/")
    if not path:
        return endpoint.rstrip("/") + "/openai/v1"
    if path.endswith("/openai/v1"):
        return endpoint.rstrip("/")
    raise ConfigError(
        "Use the Microsoft Foundry resource root or its /openai/v1 base URL, "
        "not a /models, deployment, or project-only endpoint."
    )


def normalize_legacy_api_key(data: dict[str, Any], *, section: str = "model") -> dict[str, Any]:
    if "security_key" not in data:
        return data
    result = data.copy()
    legacy = result.pop("security_key")
    if "api_key" in result and result["api_key"] != legacy:
        raise ConfigError(
            f"{section}.api_key conflicts with legacy {section}.security_key. "
            "Keep one spelling or set matching values."
        )
    result["api_key"] = legacy
    return result


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


class ModelApiMode(StrEnum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"


def validate_responses_version(version: str) -> None:
    try:
        supported = date.fromisoformat(version.removesuffix("-preview")) >= date(2025, 3, 1)
    except ValueError:
        supported = False
    if not supported:
        raise ConfigError("Azure Responses requires api_version 2025-03-01-preview or newer.")


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
    api_mode: ModelApiMode = ModelApiMode.CHAT_COMPLETIONS
    api_key: str = Field(default="", validation_alias=AliasChoices("api_key", "security_key"))
    max_input_chars: int = Field(default=24000, ge=2000)
    max_input_images: int = Field(default=5, ge=1, le=20)
    max_input_image_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    max_output_tokens: int = Field(default=2048, ge=1)
    timeout_seconds: float = Field(default=120, ge=5, le=600)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_api_key(cls, data: Any) -> Any:
        return normalize_legacy_api_key(data) if isinstance(data, dict) else data

    @model_validator(mode="after")
    def validate_foundry(self) -> DefaultAgentModelSettings:
        if self.model_provider_type is ModelProviderType.AZUREFOUNDRY:
            if self.base_endpoint:
                foundry_v1_endpoint(self.base_endpoint)
                if self.api_version and urlsplit(self.base_endpoint).path.rstrip("/").endswith("/openai/v1"):
                    raise ConfigError("Foundry /openai/v1 uses implicit versioning; leave api_version empty.")
            elif self.is_configured:
                raise ConfigError("Microsoft Foundry requires model.base_endpoint.")
        return self

    @model_validator(mode="after")
    def validate_api_mode(self) -> DefaultAgentModelSettings:
        if self.api_mode is ModelApiMode.RESPONSES and self.model_provider_type:
            if self.model_provider_type not in (
                ModelProviderType.OPENAI,
                ModelProviderType.AZUREOPENAI,
                ModelProviderType.AZUREFOUNDRY,
            ):
                raise ConfigError(
                    "Responses is supported only for OpenAI, Azure OpenAI and Microsoft Foundry."
                )
            if self.model_provider_type is ModelProviderType.AZUREOPENAI or (
                self.model_provider_type is ModelProviderType.AZUREFOUNDRY and self.api_version
            ):
                validate_responses_version(self.api_version)
        return self

    @property
    def uses_foundry_v1(self) -> bool:
        return self.model_provider_type is ModelProviderType.AZUREFOUNDRY and not self.api_version

    @property
    def is_configured(self) -> bool:
        return bool(self.model_provider_type and self.model_name)
