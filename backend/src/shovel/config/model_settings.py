from __future__ import annotations

from enum import Enum

from pydantic_settings import BaseSettings


class ModelProviderType(Enum):
    OPENAI = 1
    AZUREOPENAI = 2
    AZUREFOUNDRY = 3
    MICRSOFTCOPILOT = 4
    QIANWEN = 5
    DEEPSEEK = 6
    KIMI = 7
    OLLAMA = 8



class DefaultAgentModelSettings(BaseSettings):
    model_provider_type: ModelProviderType | None = None
    base_endpoint: str | None = None
    model_name: str | None = None
    api_version: str | None = None
    security_key: str | None = None


