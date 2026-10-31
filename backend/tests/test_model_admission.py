from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from agent_framework import Agent

from shovel.config.settings import AppSettings
from shovel.runtime.agent import create_framework_client
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.model_admission import LocalModelAdmission, ModelAdmissionTransport
from shovel.runtime.performance import RuntimePerformance


async def test_local_admission_prioritizes_foreground_and_ages_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr("shovel.runtime.model_admission.monotonic", lambda: clock[0])
    gate = LocalModelAdmission(1, 3)
    stopping = asyncio.Event()
    order: list[str] = []

    async def enter(source: str) -> None:
        async with gate.admit(source, stopping):
            order.append(source)

    async with gate.admit("platform", stopping):
        background = asyncio.create_task(enter("warmup"))
        await asyncio.sleep(0)
        foreground = asyncio.create_task(enter("foreground"))
        await asyncio.sleep(0)
    await asyncio.gather(background, foreground)
    assert order == ["foreground", "warmup"]
    order.clear()
    async with gate.admit("foreground", stopping):
        background = asyncio.create_task(enter("memory"))
        await asyncio.sleep(0)
        clock[0] += 31
        foreground = asyncio.create_task(enter("foreground"))
        await asyncio.sleep(0)
    await asyncio.gather(background, foreground)
    assert order == ["memory", "foreground"]
    assert gate.running == 0


@pytest.mark.parametrize("action", ["cancel", "stop", "timeout"])
async def test_local_admission_releases_waiters_on_cancel_stop_and_timeout(action: str) -> None:
    gate = LocalModelAdmission(1, 0.05 if action == "timeout" else 3)
    stopping = asyncio.Event()

    async def wait() -> None:
        async with gate.admit("memory", stopping):
            pytest.fail("A second request must not enter the occupied slot.")

    async with gate.admit("foreground", stopping):
        waiter = asyncio.create_task(wait())
        await asyncio.sleep(0)
        if action == "cancel":
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        else:
            if action == "stop":
                stopping.set()
            with pytest.raises(RuntimeFailure):
                await waiter
    assert gate.running == 0
    stopping.clear()
    async with gate.admit("foreground", stopping):
        assert gate.running == 1
    assert gate.running == 0


async def test_transport_holds_stream_slot_until_body_close_and_owner_close() -> None:
    gate = LocalModelAdmission(1, 3)
    stopping = asyncio.Event()
    entered: list[str] = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"first"
            await asyncio.Event().wait()

    async def respond(request: httpx.Request) -> httpx.Response:
        entered.append(request.url.path)
        return httpx.Response(200, stream=Body())

    client = httpx.AsyncClient(
        transport=ModelAdmissionTransport(gate, "foreground", stopping, httpx.MockTransport(respond))
    )
    try:
        first = await client.send(client.build_request("GET", "http://local/first"), stream=True)
        second = asyncio.create_task(
            client.send(client.build_request("GET", "http://local/second"), stream=True)
        )
        await asyncio.sleep(0)
        assert entered == ["/first"] and gate.running == 1
        await first.aclose()
        response = await second
        assert entered == ["/first", "/second"] and gate.running == 1
        await client.aclose()
        assert gate.running == 0
        await response.aclose()
        assert gate.running == 0
    finally:
        await client.aclose()


async def test_transport_error_and_cancelled_stream_release_model_slot() -> None:
    gate = LocalModelAdmission(1, 3)
    stopping = asyncio.Event()
    reading = asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            reading.set()
            await asyncio.Event().wait()
            yield b"unreachable"

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/failure":
            raise httpx.ConnectError("offline")
        return httpx.Response(200, stream=Body())

    async with httpx.AsyncClient(
        transport=ModelAdmissionTransport(gate, "foreground", stopping, httpx.MockTransport(respond))
    ) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("http://local/failure")
        assert gate.running == 0
        request = asyncio.create_task(client.get("http://local/body"))
        await reading.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert gate.running == 0


@pytest.mark.parametrize("streaming", [False, True])
async def test_real_maf_tool_child_call_does_not_hold_parent_http_lease(streaming: bool) -> None:
    settings = AppSettings(model={"model_provider_type": "ollama", "model_name": "test"})
    performance = RuntimePerformance(settings)
    calls: list[dict[str, object]] = []

    class Payload(httpx.AsyncByteStream):
        def __init__(self, content: bytes) -> None:
            self.content = content

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield self.content

    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_child", "type": "function", "function": {"name": "child", "arguments": "{}"}}
                ],
            }
        else:
            message = {"role": "assistant", "content": "child done" if len(calls) == 2 else "parent done"}
        if payload.get("stream"):
            delta = dict(message)
            if "tool_calls" in delta:
                delta["tool_calls"] = [{"index": 0, **call} for call in delta["tool_calls"]]
            chunk = {
                "id": "test",
                "created": 1,
                "model": "test",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            ending = {
                **chunk,
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "tool_calls" if len(calls) == 1 else "stop"}
                ],
            }
            content = (
                "".join("data: " + json.dumps(item) + "\n\n" for item in [chunk, ending]) + "data: [DONE]\n\n"
            )
            return httpx.Response(
                200, stream=Payload(content.encode()), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200,
            json={
                "created": 1,
                "choices": [
                    {"message": message, "finish_reason": "tool_calls" if len(calls) == 1 else "stop"}
                ],
            },
        )

    async with httpx.AsyncClient(
        transport=ModelAdmissionTransport(
            performance.models, "foreground", asyncio.Event(), httpx.MockTransport(respond)
        )
    ) as http:
        sdk, client = create_framework_client(settings.model, http, max_iterations=3)

        async def child() -> str:
            """Execute an isolated child model request."""
            with performance.models.source("platform"):
                response = await client.get_response("child request")
            return response.text

        try:
            agent = Agent(client=client, tools=[child])

            async def run() -> str:
                if not streaming:
                    return (await agent.run("parent request")).text
                parts = []
                async for update in agent.run("parent request", stream=True):
                    parts.append(update.text)
                return "".join(parts)

            result = await asyncio.wait_for(run(), 3)
            assert result == "parent done"
            assert len(calls) == 3
            assert performance.models.running == 0
        finally:
            await sdk.close()


def test_local_transport_excludes_remote_and_cloud_models() -> None:
    settings = AppSettings(model={"model_provider_type": "ollama", "model_name": "local"})
    performance = RuntimePerformance(settings)
    stopping = asyncio.Event()
    settings.model.base_endpoint = "https://remote.invalid"
    assert performance.model_transport(settings.model, "foreground", stopping) is None
    settings.model.base_endpoint = "http://127.0.0.1:11434"
    settings.model.model_name = "large:cloud"
    assert performance.model_transport(settings.model, "foreground", stopping) is None
