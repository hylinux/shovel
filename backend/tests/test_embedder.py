"""embedder 的测试, 用 httpx 的 MockTransport 顶掉网络。

这里不打真实端点: 那会让测试依赖一个 API key 和一次网络往返, 而要验证
的东西 (请求拼得对不对、响应解得对不对、失败会不会重试) 全部发生在
HTTP 的两端, 中间那一跳不提供任何信息。

MockTransport 是恰当的边界 —— 它仍然让 httpx 自己去做序列化、header
处理和超时, 被替换掉的只有"真的发出去"这一步。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from shovel.config.embedding_config import EmbeddingSettings
from shovel.exceptions.pipeline import (
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    EmbeddingNotConfiguredError,
)
from shovel.pipeline.embedder import HttpEmbedder, NullEmbedder, build_embedder

_DIM = 4


def _settings(**kwargs: Any) -> EmbeddingSettings:
    base = {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "base_url": "https://api.example.com/v1",
        "api_key": SecretStr("sk-test"),
    }
    base.update(kwargs)

    return EmbeddingSettings(**base)


def _embedder(handler: Any, **kwargs: Any) -> HttpEmbedder:
    settings = kwargs.pop("settings", None) or _settings(**kwargs)

    return HttpEmbedder(
        settings,
        expected_dimension=_DIM,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _vector(seed: int) -> list[float]:
    return [float(seed)] * _DIM


# --------------------------------------------------------------------------- #
# 解码
# --------------------------------------------------------------------------- #
async def test_openai_response_is_reordered_by_index() -> None:
    """OpenAI 只保证每项带 index, **不**保证返回顺序与输入一致。

    不排序的话每个 chunk 都会配上别人的向量, 而且完全静默 —— 索引
    看起来一切正常, 只是搜出来的东西永远牛头不对马嘴。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [
            {"index": 2, "embedding": _vector(2)},
            {"index": 0, "embedding": _vector(0)},
            {"index": 1, "embedding": _vector(1)},
        ]})

    vectors = await _embedder(handler).embed(["a", "b", "c"])

    assert vectors == [_vector(0), _vector(1), _vector(2)]


async def test_ollama_native_response_shape() -> None:
    """Ollama 用的是 ``embeddings`` 而不是 ``data``。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embed"

        return httpx.Response(200, json={"embeddings": [_vector(0), _vector(1)]})

    embedder = _embedder(handler, provider="ollama", base_url="http://127.0.0.1:11434")

    assert await embedder.embed(["a", "b"]) == [_vector(0), _vector(1)]


async def test_short_response_is_an_error_not_a_silent_misalignment() -> None:
    """少返回一个向量, 后面所有 chunk 的配对就整体错位了。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": _vector(0)}]})

    with pytest.raises(EmbeddingError):
        await _embedder(handler).embed(["a", "b"])


async def test_dimension_mismatch_is_caught_at_the_first_batch() -> None:
    """维度不符 = collection 建错了。

    必须当场拒绝: 放过去的话会先写坏一批向量, 而清理它们要重建整个库。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] * 99}]})

    with pytest.raises(EmbeddingDimensionMismatchError):
        await _embedder(handler).embed(["a"])


# --------------------------------------------------------------------------- #
# 请求拼装
# --------------------------------------------------------------------------- #
async def test_azure_puts_the_model_in_the_url() -> None:
    """Azure 把部署名放路径里, 而不是请求体。拼错就是 404。"""

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        seen["key"] = request.headers.get("api-key")

        return httpx.Response(200, json={"data": [{"index": 0, "embedding": _vector(0)}]})

    settings = _settings(
        provider="azure_openai",
        model="my-deployment",
        base_url="https://contoso.openai.azure.com",
        api_version="2024-02-01",
    )
    await _embedder(handler, settings=settings).embed(["a"])

    assert seen["path"] == "/openai/deployments/my-deployment/embeddings"
    assert "api-version=2024-02-01" in seen["query"]
    assert seen["key"] == "sk-test"


async def test_long_text_is_truncated_before_sending() -> None:
    """超长输入会被端点静默截断或直接 400。自己先截才可控。"""

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["input"] = json.loads(request.content)["input"]

        return httpx.Response(200, json={"data": [{"index": 0, "embedding": _vector(0)}]})

    embedder = _embedder(handler, max_input_chars=100)
    await embedder.embed(["x" * 5000])

    assert len(seen["input"][0]) == 100


async def test_requests_are_split_into_batches() -> None:
    """一次塞几千条会超出端点的单请求上限。"""

    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        batch = json.loads(request.content)["input"]
        sizes.append(len(batch))

        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": _vector(i)} for i in range(len(batch))
        ]})

    embedder = _embedder(handler, batch_size=2)
    vectors = await embedder.embed(["a", "b", "c", "d", "e"])

    assert sizes == [2, 2, 1]
    assert len(vectors) == 5


# --------------------------------------------------------------------------- #
# 失败处理
# --------------------------------------------------------------------------- #
async def test_rate_limit_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 是限流, 不是错误。放弃整轮扫描是不可接受的反应。"""

    monkeypatch.setattr("shovel.pipeline.embedder._backoff", lambda attempt: 0.0)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1

        if calls["n"] < 3:
            return httpx.Response(429, text="slow down")

        return httpx.Response(200, json={"data": [{"index": 0, "embedding": _vector(0)}]})

    assert await _embedder(handler).embed(["a"]) == [_vector(0)]
    assert calls["n"] == 3


async def test_retries_eventually_give_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shovel.pipeline.embedder._backoff", lambda attempt: 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    with pytest.raises(EmbeddingError):
        await _embedder(handler, max_retries=2).embed(["a"])


async def test_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """401 重试一百次还是 401, 只会把失败拖慢一百倍。"""

    monkeypatch.setattr("shovel.pipeline.embedder._backoff", lambda attempt: 0.0)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1

        return httpx.Response(401, text="bad key")

    with pytest.raises(EmbeddingError):
        await _embedder(handler).embed(["a"])

    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #
def test_build_returns_null_embedder_when_disabled() -> None:
    """``--no-embed`` 不该要求用户先配好 API key。"""

    embedder = build_embedder(EmbeddingSettings(), expected_dimension=_DIM, enabled=False)

    assert isinstance(embedder, NullEmbedder)


def test_build_refuses_to_guess_when_unconfigured() -> None:
    """没配就明确报错。悄悄回落到"不做 embedding"会让用户以为索引好了。"""

    with pytest.raises(EmbeddingNotConfiguredError):
        build_embedder(EmbeddingSettings(), expected_dimension=_DIM)


async def test_empty_input_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("不该为空输入发请求。")

    assert await _embedder(handler).embed([]) == []
