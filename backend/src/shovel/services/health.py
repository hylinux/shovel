# ---------------------------------------------------------------------
# 外部依赖连通性自检
#
# 'shovel init' 承诺"检查默认大模型的连接", 这里是它的实现。
#
# 三条贯穿性的原则:
#
# 1. 自检永远不能让 init 失败。
#    init 的任务是把本地目录/数据库/向量库准备好。外部依赖连不上时
#    仍须保留本地初始化结果, 并明确报告检查失败。
#
# 2. "没配置"和"配了但连不上"要分开报。
#    前者是正常的待办事项, 后者才是需要用户去查的问题。
#
# 3. 不引入新依赖。
#    探活复用现有 httpx 与 Azure CLI 认证客户端。
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
# ---------------------------------------------------------------------
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

import httpx
from openai import APIError, APIStatusError

from shovel.config.model_settings import (
    DefaultAgentModelSettings,
    ModelProviderType,
    foundry_v1_endpoint,
)
from shovel.runtime.errors import RuntimeFailure


class HealthStatus(Enum):
    """自检结果的三种状态。"""

    OK = "ok"

    #: 用户还没配这一项 —— 属于待办, 不是故障。
    SKIPPED = "skipped"

    #: 配了, 但连不上 / 认证失败。
    FAILED = "failed"


@dataclass(slots=True, frozen=True)
class HealthResult:
    status: HealthStatus
    target: str  # 被检查的对象, 例如 "127.0.0.1:6379"
    detail: str  # 给用户看的一句话

    @property
    def ok(self) -> bool:
        return self.status is HealthStatus.OK


# ================================================================
# 默认大模型
# ================================================================

_MODEL_TIMEOUT_SECONDS = 10.0

#: 各 provider 用来"证明自己活着"的只读端点。
#:
#: 刻意不发真正的推理请求: 那会产生费用, 而且首 token 的延迟足以让
#: init 看起来像卡死了。列模型是所有 OpenAI 兼容服务都实现的最轻接口。
_OPENAI_COMPATIBLE = {
    ModelProviderType.OPENAI,
    ModelProviderType.MICRSOFTCOPILOT,
    ModelProviderType.QIANWEN,
    ModelProviderType.DEEPSEEK,
    ModelProviderType.KIMI,
}


def check_model(settings: DefaultAgentModelSettings) -> HealthResult:
    """确认默认大模型的端点可达且凭据有效。"""

    target = settings.base_endpoint or "<unset>"

    if not settings.is_configured:
        return HealthResult(
            status=HealthStatus.SKIPPED,
            target=target,
            detail=(
                "the model section is not configured yet, skipped. "
                "Fill in model.model_provider_type / model.base_endpoint / "
                "model.model_name in the configuration file."
            ),
        )

    if not settings.base_endpoint:
        return HealthResult(
            status=HealthStatus.SKIPPED,
            target=target,
            detail=(
                "model.base_endpoint is empty, skipped. Shovel will not guess a vendor endpoint for you."
            ),
        )

    if settings.model_provider_type is ModelProviderType.AZUREOPENAI and not settings.api_version:
        return HealthResult(
            status=HealthStatus.FAILED,
            target=target,
            detail="Azure model health checks require model.api_version.",
        )

    url, headers = _probe_request(settings)
    azure_cli = (
        settings.model_provider_type in (ModelProviderType.AZUREOPENAI, ModelProviderType.AZUREFOUNDRY)
        and not settings.api_key
    )

    try:
        if azure_cli:
            asyncio.run(_probe_azure_cli(settings))
            status = 200
        else:
            response = httpx.get(
                url,
                headers=headers,
                timeout=_MODEL_TIMEOUT_SECONDS,
            )
            status = response.status_code
    except APIStatusError as exc:
        status = exc.status_code
    except RuntimeFailure as exc:
        return HealthResult(
            status=HealthStatus.FAILED,
            target=target,
            detail=f"{exc.message} {exc.hint or ''}".strip(),
        )
    except (httpx.HTTPError, httpx.InvalidURL, APIError) as exc:
        return HealthResult(
            status=HealthStatus.FAILED,
            target=target,
            detail=f"Cannot reach the model endpoint {url} ({type(exc).__name__}).",
        )

    if status in (401, 403):
        guidance = (
            "Check Azure CLI login, resource tenant and Azure model access permissions."
            if azure_cli
            else "Check model.api_key."
        )
        return HealthResult(
            status=HealthStatus.FAILED,
            target=target,
            detail=f"The model endpoint rejected the credential (HTTP {status}). {guidance}",
        )

    if status >= 400:
        return HealthResult(
            status=HealthStatus.FAILED,
            target=target,
            detail=f"The model endpoint returned HTTP {status} for {url}.",
        )

    return HealthResult(
        status=HealthStatus.OK,
        target=target,
        detail=f"Model endpoint {settings.base_endpoint} is reachable.",
    )


async def _probe_azure_cli(settings: DefaultAgentModelSettings) -> None:
    from shovel.runtime.azure_auth import AzureCliOpenAIClient, FoundryCliOpenAIClient

    if settings.uses_foundry_v1:
        async with FoundryCliOpenAIClient(
            base_url=foundry_v1_endpoint(settings.base_endpoint),
            timeout=_MODEL_TIMEOUT_SECONDS,
            max_retries=0,
        ) as client:
            await client.models.list()
        return

    async with AzureCliOpenAIClient(
        azure_endpoint=settings.base_endpoint,
        api_version=settings.api_version,
        api_key=None,
        timeout=_MODEL_TIMEOUT_SECONDS,
        max_retries=0,
    ) as client:
        await client.models.list()


def _probe_request(
    settings: DefaultAgentModelSettings,
) -> tuple[str, dict[str, str]]:
    """按 provider 给出探活 URL 与请求头。"""

    base = settings.base_endpoint.rstrip("/")
    provider = settings.model_provider_type
    key = settings.api_key

    if settings.uses_foundry_v1:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return f"{foundry_v1_endpoint(base)}/models", headers

    if provider is ModelProviderType.OLLAMA:
        # Ollama 没有鉴权, 原生端点也比 /v1 兼容层更可靠
        return f"{base}/api/tags", {}

    if provider in (
        ModelProviderType.AZUREOPENAI,
        ModelProviderType.AZUREFOUNDRY,
    ):
        headers = {"api-key": key} if key else {}

        return f"{base}/openai/models?api-version={settings.api_version}", headers

    if provider in _OPENAI_COMPATIBLE:
        headers = {"Authorization": f"Bearer {key}"} if key else {}

        return f"{base}/models", headers

    # 未知 provider: 按 OpenAI 兼容处理, 这是兼容性最好的猜测
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    return f"{base}/models", headers


__all__ = [
    "HealthResult",
    "HealthStatus",
    "check_model",
]
