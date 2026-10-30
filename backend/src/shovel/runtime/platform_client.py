from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from shovel.config.settings import AppSettings
from shovel.platform.domain import PlatformCommand
from shovel.platform.service import CONFIRMED_ACTIONS
from shovel.runtime.client import _decode, _remote
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.hosting import build_cli_host, resolve_runtime

type Send = Callable[[PlatformCommand], Awaitable[dict[str, Any]]]


async def _perform(
    send: Send,
    command: PlatformCommand,
    confirm: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    if command.expected_revision is None and command.action in {
        "capability.publish",
        "proactive.enable",
        "proactive.resume",
        "proactive.pause",
        "proactive.disable",
    }:
        target = await send(
            PlatformCommand(
                action="capability.show" if command.action == "capability.publish" else "proactive.show",
                identifier=command.identifier,
                version=command.version,
            )
        )
        command = command.model_copy(update={"expected_revision": target["revision"]})
    if command.action in CONFIRMED_ACTIONS:
        preview = await send(command.model_copy(update={"prepare": True}))
        if not await asyncio.to_thread(confirm, preview):
            raise RuntimeFailure("操作已取消。")
        command = command.model_copy(update={"confirmation_id": preview["confirmation_id"]})
    return await send(command)


async def platform_command(
    settings: AppSettings,
    command: PlatformCommand,
    *,
    confirm: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        url = await _remote(settings, client)
        if url is not None:

            async def remote(body: PlatformCommand) -> dict[str, Any]:
                try:
                    return _decode(
                        await client.post(
                            f"{url}/api/v1/platform/commands",
                            json=body.model_dump(mode="json"),
                        )
                    )
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    raise RuntimeFailure(
                        "Host 连接中断,结果未知;请检查状态,不要盲目重复操作。",
                        hint=f"request_key={body.request_key}; action={body.action}",
                        status=503,
                    ) from exc

            return await _perform(remote, command, confirm)
    host, provider = build_cli_host(settings)
    try:
        await host.start_async()
        runtime = await resolve_runtime(provider)

        async def local(body: PlatformCommand) -> dict[str, Any]:
            return await runtime.platform.execute(body, human=True)

        result = await _perform(local, command, confirm)
        if command.action in {"execution.run", "proactive.run", "execution.resume"}:
            identifier = result["id"]
            while result["state"] in {"queued", "running"}:
                if runtime.stopping.is_set():
                    raise RuntimeFailure("Host stopped; the Execution remains durable.", status=503)
                await asyncio.sleep(0.05)
                result = await local(PlatformCommand(action="execution.show", identifier=identifier))
        return result
    finally:
        host.lifetime.stop_application()
        await host.stop_async()
