# ---------------------------------------------------------
# 文本向量化
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
# ---------------------------------------------------------
"""MAF embedding clients shared by knowledge and Memory, with bounded retries."""

from __future__ import annotations

import asyncio
import math
import random
from typing import Any, Protocol

import httpx
from agent_framework.ollama import OllamaEmbeddingClient
from agent_framework.openai import OpenAIEmbeddingClient, OpenAIEmbeddingOptions
from ollama import AsyncClient as OllamaAsyncClient
from ollama import ResponseError
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    AsyncAzureOpenAI,
    AsyncOpenAI,
    omit,
)
from openai._models import FinalRequestOptions

from shovel.config.embedding_config import EmbeddingSettings
from shovel.exceptions.pipeline import (
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    EmbeddingNotConfiguredError,
)
from shovel.protection.egress import EgressChannel, require_ordinary
from shovel.runtime.azure_auth import AzureCliOpenAIClient
from shovel.runtime.errors import RuntimeFailure

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


class _ClientTransport(httpx.AsyncBaseTransport):
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        require_ordinary(EgressChannel.EMBEDDING)
        return await self.client.send(request, stream=True)


class _AnonymousOpenAIClient(AsyncOpenAI):
    async def _prepare_options(self, options: FinalRequestOptions) -> FinalRequestOptions:
        options = await super()._prepare_options(options)
        options.headers = {**(options.headers or {}), "Authorization": omit}
        return options


class HttpEmbedder:
    """Keep the existing adapter interface while delegating generation to MAF."""

    def __init__(
        self,
        settings: EmbeddingSettings,
        *,
        expected_dimension: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not settings.is_configured:
            raise EmbeddingNotConfiguredError()
        if settings.dimensions is not None and settings.dimensions != expected_dimension:
            raise EmbeddingError(
                settings.provider,
                f"Configured dimensions {settings.dimensions} must match index dim {expected_dimension}.",
            )

        self._settings = settings
        self._expected = expected_dimension
        self._dimension = 0
        self._client = client
        self._owns_client = client is None
        self._sdk: AsyncOpenAI | None = None
        self._ollama: OllamaAsyncClient | None = None
        self._framework: OpenAIEmbeddingClient | OllamaEmbeddingClient | None = None

    # ----------------------------------------------------------------- #
    # 协议
    # ----------------------------------------------------------------- #
    @property
    def model_version(self) -> str:
        import hashlib
        import json

        identity = {
            "endpoint": self._settings.base_url.rstrip("/"),
            "api_version": self._settings.api_version,
            "revision": self._settings.revision,
            "dimension": self._expected,
            "max_input_chars": self._settings.max_input_chars,
        }
        if self._settings.dimensions is not None:
            identity["dimensions"] = self._settings.dimensions
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
        return f"{self._settings.provider}:{self._settings.model}@{fingerprint}"

    @property
    def dimension(self) -> int:
        # 未发过请求时回落到配置的期望值: 调用方 (Zvec 写入) 需要在
        # 第一次 embed 之前就知道该建多宽的字段
        return self._dimension or self._expected

    async def embed(self, texts: list[str]) -> list[list[float]]:
        require_ordinary(EgressChannel.EMBEDDING, texts)
        if not texts:
            return []

        limit = self._settings.max_input_chars
        payload = [t[:limit] if len(t) > limit else t for t in texts]

        vectors: list[list[float]] = []

        for start in range(0, len(payload), self._settings.batch_size):
            batch = payload[start : start + self._settings.batch_size]
            vectors.extend(await self._embed_batch(batch))

        if vectors:
            self._dimension = self._expected
        return vectors

    async def aclose(self) -> None:
        try:
            if self._sdk is not None and self._owns_client:
                await self._sdk.close()
            elif isinstance(self._sdk, AzureCliOpenAIClient):
                await self._sdk.close_credential()
            if self._ollama is not None:
                # The Ollama SDK exposes no public close method.
                await self._ollama._client.aclose()
        finally:
            self._framework = None
            self._sdk = None
            self._ollama = None
            if self._owns_client and self._client is not None:
                if not self._client.is_closed:
                    await self._client.aclose()
                self._client = None

    # ----------------------------------------------------------------- #
    # 内部
    # ----------------------------------------------------------------- #
    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        require_ordinary(EgressChannel.EMBEDDING, batch)
        attempt = 0

        while True:
            try:
                framework = self._framework_client()
                if isinstance(framework, OpenAIEmbeddingClient):
                    options: OpenAIEmbeddingOptions = {"encoding_format": "float"}
                    if self._settings.dimensions is not None:
                        options["dimensions"] = self._settings.dimensions
                    result = await framework.get_embeddings(batch, options=options)
                    raw_vectors = [item.vector for item in result]
                else:
                    raw_vectors = [item.vector for item in await framework.get_embeddings(batch)]
                if len(raw_vectors) != len(batch):
                    raise EmbeddingError(
                        self._settings.provider,
                        f"请求了 {len(batch)} 条文本, 返回了 {len(raw_vectors)} 个向量。",
                    )
                vectors = [[float(value) for value in vector] for vector in raw_vectors]
                for vector in vectors:
                    if len(vector) != self._expected:
                        raise EmbeddingDimensionMismatchError(self._expected, len(vector))
                    if not all(math.isfinite(value) for value in vector):
                        raise EmbeddingError(self._settings.provider, "Embedding contains non-finite values.")
                return vectors
            except (APIStatusError, ResponseError) as exc:
                if exc.status_code not in _RETRYABLE_STATUS:
                    raise EmbeddingError(self._settings.provider, f"HTTP {exc.status_code}") from None
                reason = f"HTTP {exc.status_code}"
            except APIConnectionError, httpx.TransportError, ConnectionError:
                reason = "Embedding transport failed."
            except RuntimeFailure as exc:
                reason = f"{exc.message} {exc.hint or ''}".strip()
                raise EmbeddingError(self._settings.provider, reason) from None
            except APIResponseValidationError, ValueError, TypeError, KeyError:
                raise EmbeddingError(self._settings.provider, "Invalid embedding response.") from None

            attempt += 1
            if attempt > self._settings.max_retries:
                raise EmbeddingError(
                    self._settings.provider,
                    f"重试 {self._settings.max_retries} 次后仍失败: {reason}",
                )
            await asyncio.sleep(_backoff(attempt))

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.timeout_seconds)
        return self._client

    def _framework_client(self) -> OpenAIEmbeddingClient | OllamaEmbeddingClient:
        if self._framework is not None:
            return self._framework
        settings = self._settings
        client = self._ensure_client()
        if _guard_embedding_request not in client.event_hooks["request"]:
            client.event_hooks["request"].append(_guard_embedding_request)
        key = settings.api_key.get_secret_value()
        base = settings.base_url.rstrip("/")
        common: dict[str, Any] = {
            "http_client": client,
            "timeout": settings.timeout_seconds,
            "max_retries": 0,
        }
        if settings.provider == "ollama":
            self._ollama = OllamaAsyncClient(
                host=base or "http://127.0.0.1:11434",
                transport=_ClientTransport(client),
                timeout=settings.timeout_seconds,
                headers={"Authorization": f"Bearer {key}"} if key else {},
            )
            self._framework = OllamaEmbeddingClient(model=settings.model, client=self._ollama)
        elif settings.provider == "azure_openai":
            if not base:
                raise EmbeddingError(settings.provider, "Azure embedding requires base_url.")
            if key:
                self._sdk = AsyncAzureOpenAI(
                    azure_endpoint=base,
                    api_version=settings.api_version,
                    api_key=key,
                    **common,
                )
            else:
                self._sdk = AzureCliOpenAIClient(
                    azure_endpoint=base,
                    api_version=settings.api_version,
                    api_key=None,
                    **common,
                    egress_channel=EgressChannel.EMBEDDING,
                )
            self._framework = OpenAIEmbeddingClient(model=settings.model, async_client=self._sdk)
        else:
            defaults = {
                "openai": "https://api.openai.com/v1",
                "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "qianwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            }
            if not base:
                base = defaults.get(settings.provider, "")
            if not base:
                raise EmbeddingError(
                    settings.provider,
                    "Configure base_url for a service that supports the OpenAI embeddings API.",
                )
            sdk_type = AsyncOpenAI if key else _AnonymousOpenAIClient
            self._sdk = sdk_type(
                api_key=key or "local-no-key",
                base_url=base,
                **common,
            )
            self._framework = OpenAIEmbeddingClient(model=settings.model, async_client=self._sdk)
        return self._framework


async def _guard_embedding_request(request: httpx.Request) -> None:
    require_ordinary(EgressChannel.EMBEDDING)


def _backoff(attempt: int) -> float:
    """指数退避 + 抖动。

    抖动不是装饰: 一次扫描里几十个并发请求被同时限流, 如果退避时间
    完全相同, 它们会在同一毫秒再次撞上去, 于是限流永远不会缓解。
    """

    base = min(2.0**attempt, 30.0)

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
