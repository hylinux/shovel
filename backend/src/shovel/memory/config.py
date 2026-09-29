#---------------------------------------------------------------------
# 把 Shovel 的配置翻译成 mem0 的配置
#
# mem0 只吃一个嵌套 dict(MemoryConfig), 而 Shovel 的配置是给人看的 TOML。
# 这层翻译单独成文件, 是为了让"mem0 又改了字段名"这件事只影响一个地方。
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------------------
from __future__ import annotations

from pathlib import Path
from typing import Any

from shovel.config.memory_config import (
    MemoryEmbedderSettings,
    MemoryLlmSettings,
    MemorySettings,
)
from shovel.config.model_settings import DefaultAgentModelSettings, ModelProviderType

#: Shovel 的 provider 枚举 -> mem0 的 provider 名。
#:
#: 通义/Kimi/Copilot 都落到 "openai": 它们提供的是 OpenAI 兼容端点,
#: mem0 没有对应的 provider, 走 openai + base_url 才是正确接法 ——
#: 给 mem0 传一个它不认识的名字, 报错会发生在 Memory() 构造那一刻,
#: 离用户写下配置已经很远了。
_PROVIDERS: dict[ModelProviderType, str] = {
    ModelProviderType.OPENAI: "openai",
    ModelProviderType.AZUREOPENAI: "azure_openai",
    ModelProviderType.AZUREFOUNDRY: "azure_openai",
    ModelProviderType.MICRSOFTCOPILOT: "openai",
    ModelProviderType.QIANWEN: "openai",
    ModelProviderType.DEEPSEEK: "deepseek",
    ModelProviderType.KIMI: "openai",
    ModelProviderType.OLLAMA: "ollama",
}

#: mem0 的 base_url 参数名是按 provider 各起各的(BaseLlmConfig 里根本没有
#: 统一的 base_url), 传错了不会报错, 只会静默连去官方端点。
_BASE_URL_KEYS: dict[str, str] = {
    "openai": "openai_base_url",
    "ollama": "ollama_base_url",
    "deepseek": "deepseek_base_url",
    "lmstudio": "lmstudio_base_url",
    "huggingface": "huggingface_base_url",
}

#: 没有任何配置时的兜底。mem0 自己的默认值也是 openai,
#: 这里写出来只是为了让报错信息能指向具体配置项。
_DEFAULT_PROVIDER = "openai"


def _provider_of(model: DefaultAgentModelSettings) -> str:
    if not model.model_provider_type:
        return _DEFAULT_PROVIDER

    return _PROVIDERS.get(model.model_provider_type, _DEFAULT_PROVIDER)


def _apply_endpoint(
        config: dict[str, Any],
        provider: str,
        *,
        base_url: str | None,
        api_key: str | None,
        api_version: str | None,
        deployment: str | None,
) -> None:
    """按 provider 的规矩把端点信息写进 config。

    Azure 是唯一的例外: 它不吃 base_url, 而是要一整个 azure_kwargs
    (endpoint + deployment + api_version), 少一项就连不上。
    """

    if provider == "azure_openai":
        azure: dict[str, Any] = {}

        if base_url:
            azure["azure_endpoint"] = base_url
        if api_key:
            azure["api_key"] = api_key
        if api_version:
            azure["api_version"] = api_version
        if deployment:
            azure["azure_deployment"] = deployment

        if azure:
            config["azure_kwargs"] = azure

        return

    if api_key:
        config["api_key"] = api_key

    if base_url:
        config[_BASE_URL_KEYS.get(provider, "base_url")] = base_url


def _llm_section(
        llm: MemoryLlmSettings,
        model: DefaultAgentModelSettings,
) -> dict[str, Any]:
    """[memory.llm] 未显式配置的项, 回落到 [model] 的默认大模型。

    记忆抽取用的模型与对话模型天然可以不同(抽取用更便宜的就够了),
    所以它是可覆盖的; 但绝大多数用户不会去配, 因此必须有回落。
    """

    provider = llm.provider or _provider_of(model)
    name = llm.model or model.model_name

    config: dict[str, Any] = {
        "temperature": llm.temperature,
        "max_tokens": llm.max_tokens,
    }

    if name:
        config["model"] = name

    _apply_endpoint(
        config,
        provider,
        base_url=llm.base_url or model.base_endpoint,
        api_key=llm.api_key or model.security_key,
        api_version=model.api_version,
        deployment=name,
    )

    return {"provider": provider, "config": config}


def _embedder_section(
        embedder: MemoryEmbedderSettings,
        model: DefaultAgentModelSettings,
        dim: int,
) -> dict[str, Any]:
    """embedding 维度必须与 Qdrant collection 一致, 所以由 dim 单点给出。

    注意 embedding 模型没有回落到 [model].model_name: 对话模型的名字
    拿去做 embedding 一定会失败, 宁可让 mem0 用它自己的默认 embedding 模型。
    """

    provider = embedder.provider or _provider_of(model)

    config: dict[str, Any] = {"embedding_dims": dim}

    if embedder.model:
        config["model"] = embedder.model

    _apply_endpoint(
        config,
        provider,
        base_url=embedder.base_url or model.base_endpoint,
        api_key=embedder.api_key or model.security_key,
        api_version=model.api_version,
        deployment=embedder.model,
    )

    return {"provider": provider, "config": config}


def build_memory_config(
        settings: MemorySettings,
        model: DefaultAgentModelSettings,
) -> dict[str, Any]:
    """生成可直接喂给 ``Memory.from_config()`` 的 dict。"""

    return {
        "vector_store": {
            "provider": "qdrant",
            "config": settings.qdrant.client_kwargs(
                collection=settings.collection,
                dim=settings.dim,
            ),
        },
        "llm": _llm_section(settings.llm, model),
        "embedder": _embedder_section(settings.embedder, model, settings.dim),
        "history_db_path": settings.history_db_path,
    }


def memory_paths(settings: MemorySettings) -> list[Path]:
    """mem0 会落盘的目录, 用于在初始化前先把它们建出来。

    mem0 / qdrant-client 在父目录不存在时的报错相当晦涩,
    先建目录比事后解释异常便宜得多。
    """

    paths = [Path(settings.history_db_path).expanduser().parent]

    if settings.qdrant.mode == "local":
        paths.append(Path(settings.qdrant.path).expanduser())

    return paths


__all__ = ["build_memory_config", "memory_paths"]
