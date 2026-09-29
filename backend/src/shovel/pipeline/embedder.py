#---------------------------------------------------------
# 文本向量化
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""把文本批量变成向量。

## 为什么自己发 HTTP 而不用 openai SDK

需要的只有一个 POST ``/embeddings`` 和一个 POST ``/api/embed``, 加起来
不到一百行。引 SDK 换来的是它的全部依赖树、它的重试策略 (与我们想要的
不同)、以及它对 Azure 的一套单独封装。httpx 本来就在依赖里。

更实际的一点: 这里需要精确控制"哪些错误可以重试"。SDK 的默认重试会把
400 也重试掉, 而 400 通常意味着"你的输入太长了", 重试三次只是把同一个
错误慢三倍地报出来。

## 维度校验只在第一批做

每批都校验是浪费 —— 同一个模型不会中途改维度。但完全不校验则会让一批
维度不符的向量写进 Zvec, 而 Zvec 在写入时未必会拒绝, 于是错误要等到
用户搜索时才暴露。第一批校验一次, 成本几乎为零, 覆盖了全部风险。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Protocol

from shovel.config.embedding_config import EmbeddingSettings
from shovel.exceptions.pipeline import (
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    EmbeddingNotConfiguredError,
)

#: 值得重试的 HTTP 状态码。
#:
#: 429 (限流) 和 5xx (服务端故障) 是暂时的; 408/409 同理。
#: 401/403 (认证) 和 400/422 (请求本身有问题) 不在列 —— 重试它们
#: 只会让用户多等三倍时间看到同一条错误。
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class Embedder(Protocol):
    """向量化器。

    只有一个方法, 且是**批量**的。刻意不提供单条接口: 单条调用会让
    调用方自然写出 for 循环, 于是一万个 chunk 变成一万次 HTTP 往返,
    比批量慢一到两个数量级。想要单条的人传一个长度为 1 的列表即可。
    """

    @property
    def model_version(self) -> str:
        """写进 ``chunk.model_version``。

        换模型必须换这个值: 它是"哪些向量该作废"的唯一判据。
        """
        ...

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


class HttpEmbedder:
    """OpenAI 兼容 / Azure OpenAI / Ollama 三种端点的统一实现。

    三者的差异只在 URL 拼法、鉴权头和响应结构上, 值不得为每一种写一个类:
    那样三份重试逻辑、三份维度校验会各自漂移。
    """

    def __init__(
        self,
        settings: EmbeddingSettings,
        *,
        expected_dimension: int,
        client: Any | None = None,
    ) -> None:
        if not settings.is_configured:
            raise EmbeddingNotConfiguredError()

        self._settings = settings
        self._expected = expected_dimension
        self._dimension = 0
        self._client = client
        self._owns_client = client is None

    # ----------------------------------------------------------------- #
    # 协议
    # ----------------------------------------------------------------- #
    @property
    def model_version(self) -> str:
        return f"{self._settings.provider}:{self._settings.model}"

    @property
    def dimension(self) -> int:
        # 未发过请求时回落到配置的期望值: 调用方 (Zvec 写入) 需要在
        # 第一次 embed 之前就知道该建多宽的字段
        return self._dimension or self._expected

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        limit = self._settings.max_input_chars
        payload = [t[:limit] if len(t) > limit else t for t in texts]

        vectors: list[list[float]] = []

        for start in range(0, len(payload), self._settings.batch_size):
            batch = payload[start:start + self._settings.batch_size]
            vectors.extend(await self._embed_batch(batch))

        if vectors and self._dimension == 0:
            self._dimension = len(vectors[0])

            if self._dimension != self._expected:
                raise EmbeddingDimensionMismatchError(self._expected, self._dimension)

        return vectors

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ----------------------------------------------------------------- #
    # 内部
    # ----------------------------------------------------------------- #
    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        import httpx

        url, headers, body = self._request(batch)
        client = self._ensure_client()
        attempt = 0

        while True:
            try:
                response = await client.post(url, headers=headers, json=body)

                if response.status_code in _RETRYABLE_STATUS:
                    raise _Retryable(
                        f"HTTP {response.status_code}: {response.text[:200]}"
                    )

                if response.status_code >= 400:
                    raise EmbeddingError(
                        self._settings.provider,
                        f"HTTP {response.status_code}: {response.text[:500]}",
                    )

                return self._decode(response.json(), len(batch))

            except (_Retryable, httpx.TimeoutException, httpx.TransportError) as exc:
                attempt += 1

                if attempt > self._settings.max_retries:
                    raise EmbeddingError(
                        self._settings.provider,
                        f"重试 {self._settings.max_retries} 次后仍失败: {exc}",
                    ) from exc

                await asyncio.sleep(_backoff(attempt))

    def _ensure_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                timeout=self._settings.timeout_seconds,
            )

        return self._client

    def _request(self, batch: list[str]) -> tuple[str, dict[str, str], dict[str, Any]]:
        settings = self._settings
        provider = settings.provider
        base = settings.base_url.rstrip("/")
        key = settings.api_key.get_secret_value() if settings.api_key else ""

        if provider == "ollama":
            # Ollama 原生端点。它也提供 /v1 的 OpenAI 兼容层, 但原生端点
            # 才支持批量 (兼容层的 input 只吃单条), 而批量正是这里要的。
            return (
                f"{base or 'http://127.0.0.1:11434'}/api/embed",
                {},
                {"model": settings.model, "input": batch},
            )

        if provider == "azure_openai":
            # Azure 把模型名放进 URL 路径 (deployment), 而不是请求体
            return (
                f"{base}/openai/deployments/{settings.model}/embeddings"
                f"?api-version={settings.api_version}",
                {"api-key": key},
                {"input": batch},
            )

        # openai 及一切兼容端点 (通义、DeepSeek、vLLM、LM Studio ...)
        return (
            f"{base or 'https://api.openai.com/v1'}/embeddings",
            {"Authorization": f"Bearer {key}"} if key else {},
            {"model": settings.model, "input": batch},
        )

    def _decode(self, payload: dict[str, Any], expected_count: int) -> list[list[float]]:
        provider = self._settings.provider

        if provider == "ollama":
            vectors = payload.get("embeddings") or []
        else:
            # OpenAI 不保证返回顺序与输入一致, 只保证每项带 index。
            # 不按 index 排序的话, 一旦服务端乱序, 每个 chunk 都会
            # 配上别人的向量 —— 而且这种错误完全静默。
            items = sorted(
                payload.get("data") or [],
                key=lambda item: item.get("index", 0),
            )
            vectors = [item.get("embedding") or [] for item in items]

        if len(vectors) != expected_count:
            raise EmbeddingError(
                provider,
                f"请求了 {expected_count} 条文本, 返回了 {len(vectors)} 个向量。",
            )

        return [[float(x) for x in vector] for vector in vectors]


class _Retryable(Exception):
    """内部信号: 这个错误值得重试。"""


def _backoff(attempt: int) -> float:
    """指数退避 + 抖动。

    抖动不是装饰: 一次扫描里几十个并发请求被同时限流, 如果退避时间
    完全相同, 它们会在同一毫秒再次撞上去, 于是限流永远不会缓解。
    """

    base = min(2.0 ** attempt, 30.0)

    return base * (0.5 + random.random() * 0.5)  # noqa: S311


class NullEmbedder:
    """什么都不做的 embedder。

    给 ``--no-embed`` 用: 此时 chunk 照常写进 SQLite 并标成
    ``vector_state=pending``, 等 embedding 配好之后补跑即可。
    "先把文本索引起来, 向量以后再说"是一个合理的中间状态, 值得被支持。
    """

    @property
    def model_version(self) -> str:
        return "none"

    @property
    def dimension(self) -> int:
        return 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return []

    async def aclose(self) -> None:
        return None


def build_embedder(
    settings: EmbeddingSettings,
    *,
    expected_dimension: int,
    enabled: bool = True,
) -> Embedder:
    """按配置造一个 embedder。"""

    if not enabled:
        return NullEmbedder()

    if not settings.is_configured:
        raise EmbeddingNotConfiguredError()

    return HttpEmbedder(settings, expected_dimension=expected_dimension)


__all__ = [
    "Embedder",
    "HttpEmbedder",
    "NullEmbedder",
    "build_embedder",
]
