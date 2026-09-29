#---------------------------------------------------------------------
# 外部依赖连通性自检
#
# 'shovel init' 承诺"检查默认大模型的连接", 这里是它的实现。
#
# 三条贯穿性的原则:
#
# 1. 自检永远不能让 init 失败。
#    init 的任务是把本地目录/数据库/向量库准备好, 而外部依赖在用户
#    刚装完还没填 API Key 时必然是连不上的。把"没配"当成错误, 等于
#    要求用户先手写配置文件才能跑初始化。
#
# 2. "没配置"和"配了但连不上"要分开报。
#    前者是正常的待办事项, 后者才是需要用户去查的问题。
#
# 3. 不引入新依赖。
#    探活复用已经在用的 httpx。
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------------------
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from shovel.config.model_settings import (
    DefaultAgentModelSettings,
    ModelProviderType,
)


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
    target: str      # 被检查的对象, 例如 "127.0.0.1:6379"
    detail: str      # 给用户看的一句话

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
                "model.base_endpoint is empty, skipped. "
                "Shovel will not guess a vendor endpoint for you."
            ),
        )

    url, headers = _probe_request(settings)

    import httpx

    try:
        response = httpx.get(
            url,
            headers=headers,
            timeout=_MODEL_TIMEOUT_SECONDS,
        )

    except Exception as exc:
        return HealthResult(
            status=HealthStatus.FAILED,
            target=target,
            detail=f"Cannot reach the model endpoint {url}: {exc}",
        )

    if response.status_code in (401, 403):
        return HealthResult(
            status=HealthStatus.FAILED,
            target=target,
            detail=(
                f"The model endpoint rejected the credential "
                f"(HTTP {response.status_code}). Check model.security_key."
            ),
        )

    if response.status_code >= 400:
        return HealthResult(
            status=HealthStatus.FAILED,
            target=target,
            detail=(
                f"The model endpoint returned HTTP {response.status_code} "
                f"for {url}."
            ),
        )

    return HealthResult(
        status=HealthStatus.OK,
        target=target,
        detail=f"Model endpoint {settings.base_endpoint} is reachable.",
    )


def _probe_request(
        settings: DefaultAgentModelSettings,
) -> tuple[str, dict[str, str]]:
    """按 provider 给出探活 URL 与请求头。"""

    base = settings.base_endpoint.rstrip("/")
    provider = settings.model_provider_type
    key = settings.security_key

    if provider is ModelProviderType.OLLAMA:
        # Ollama 没有鉴权, 原生端点也比 /v1 兼容层更可靠
        return f"{base}/api/tags", {}

    if provider in (
        ModelProviderType.AZUREOPENAI,
        ModelProviderType.AZUREFOUNDRY,
    ):
        # Azure 的 api-version 是必填的, 缺了会被直接拒绝
        version = settings.api_version or "2024-10-21"
        headers = {"api-key": key} if key else {}

        return f"{base}/openai/models?api-version={version}", headers

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
