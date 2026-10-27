from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import structlog
from dependency_injector import providers
from fastapi import FastAPI
from py_generic_host.di.protocols import AppContainerProtocol
from py_generic_host.hosting.builder import WebHostBuilder
from py_generic_host.hosting.host import Host
from py_generic_host.hosting.hosted_service import BackgroundService
from py_generic_host.hosting.lifetime import ApplicationLifetime
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from shovel.config.settings import AppSettings
from shovel.container import AppContainer
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.locking import endpoint_file
from shovel.runtime.service import Runtime


async def resolve_runtime(provider: Callable[[], object]) -> Runtime:
    value = provider()
    if inspect.isawaitable(value):
        value = await value
    if not isinstance(value, Runtime):
        raise RuntimeError("Runtime provider is not initialized.")
    return value


def make_container(settings: AppSettings) -> AppContainer:
    container = AppContainer()
    # Preserve SecretStr values; JSON serialization would replace keys with masks.
    container.config.from_dict(settings.model_dump(mode="python"))
    container.runtime_settings.override(providers.Object(settings))
    return container


class RuntimeWorker(BackgroundService):  # type: ignore[misc]  # Framework does not ship py.typed.
    def __init__(self, provider: Callable[[], object], *, url: str | None = None) -> None:
        super().__init__(stop_timeout=45)
        self._provider = provider
        self._runtime: Runtime | None = None
        self._url = url
        self._endpoint: Path | None = None

    async def start(self, stopping: asyncio.Event) -> None:
        self._runtime = await resolve_runtime(self._provider)
        self._runtime.stopping = stopping
        if self._url:
            self._endpoint = endpoint_file(self._runtime.settings)
            temporary = self._endpoint.with_suffix(f".{os.getpid()}.tmp")
            document = {
                "url": self._url,
                "pid": os.getpid(),
                "database_url": self._runtime.settings.database.url,
            }
            await asyncio.to_thread(temporary.write_text, json.dumps(document), encoding="utf-8")
            temporary.replace(self._endpoint)
        await super().start(stopping)

    async def execute_async(self, stopping: asyncio.Event) -> None:
        assert self._runtime is not None
        runtime = self._runtime
        runtime.worker_state = "running"
        try:
            while not stopping.is_set():
                runtime.wake.clear()
                if await runtime.operations.run_once():
                    continue
                try:
                    await asyncio.wait_for(runtime.wake.wait(), timeout=1)
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.worker_state = "failed"
            runtime.worker_error = runtime.redact(f"{type(exc).__name__}: {exc}")
            structlog.get_logger("shovel.worker").error("worker.failed", error=runtime.worker_error)
            stopping.set()
            raise
        finally:
            if runtime.worker_state != "failed":
                runtime.worker_state = "stopped"

    async def stop(self, stopping: asyncio.Event) -> None:
        if self._runtime is not None:
            self._runtime.wake.set()
        try:
            await super().stop(stopping)
        finally:
            if self._endpoint is not None:
                self._endpoint.unlink(missing_ok=True)


class FrontendFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or Path(path).suffix or path.startswith("_next/"):
                raise
            return await super().get_response("index.html", scope)
        return response


def mount_frontend(app: FastAPI, directory: Path) -> None:
    directory = directory.expanduser().resolve()
    if not (directory / "index.html").is_file():
        raise RuntimeFailure(
            f"生产前端产物不存在: {directory}",
            hint="先在 frontend 目录运行 npm run build,再将 frontend_path 指向导出目录。",
        )
    app.mount("/", FrontendFiles(directory=str(directory), html=True), name="frontend")


def build_host(
    settings: AppSettings,
    *,
    development: bool,
    frontend: Path | None = None,
) -> Host:
    from shovel.runtime.api import configure_api

    host = settings.dev_http_host if development else settings.http_host
    port = settings.dev_http_port if development else settings.http_port
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeFailure("当前个人 Runtime 仅支持 loopback 监听,不支持暴露到局域网或公网。")
    if frontend is not None and not (frontend / "index.html").is_file():
        raise RuntimeFailure(f"生产前端产物不存在: {frontend},请先执行 npm run build。")
    container = make_container(settings)
    address = f"[{host}]" if ":" in host else host
    url = f"http://{address}:{port}"
    worker = RuntimeWorker(container.runtime, url=url)
    builder = (
        WebHostBuilder[AppSettings]()
        .use_settings(settings)
        .use_container(cast(AppContainerProtocol, container))
        .enable_health_checks(False)
        .use_urls(host, port)
        .add_hosted_service(worker)
        .configure_endpoint(lambda _ctx, app: configure_api(app, container.runtime, settings))
    )
    if frontend is not None:
        builder.configure_endpoint(lambda _ctx, app: mount_frontend(app, frontend))
    return builder.build()


def build_cli_host(settings: AppSettings) -> tuple[Host, Callable[[], object]]:
    container = make_container(settings)
    lifetime = ApplicationLifetime()
    host = Host(
        container=container,
        services=[RuntimeWorker(container.runtime)],
        lifetime=lifetime,
    )
    return host, container.runtime


def local_endpoint(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and not parsed.username
        and not parsed.password
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    )
