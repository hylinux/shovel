from __future__ import annotations

import asyncio
import json
import signal
import socket
import sys
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import typer
from py_generic_host.hosting.host import HostOptions
from py_generic_host.hosting.lifetime import ApplicationLifetime
from py_generic_host.hosting.stop_reason import StopReason

from shovel.cli.commands import run as run_module
from shovel.config.embedding_config import EmbeddingSettings
from shovel.pipeline.sink import ZvecChunkSink, open_chunk_sink
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.hosting import RuntimeWarmupWorker, build_cli_host, build_host, resolve_runtime
from shovel.runtime.knowledge_worker import KnowledgeWorker
from shovel.runtime.locking import RuntimeLock, endpoint_file
from shovel.runtime.service import Runtime
from shovel.runtime.shutdown import RuntimeHost, ShutdownOutcome
from shovel.vector.store import init_vector_store
from test_hosted_runtime import runtime as runtime
from test_hosted_runtime import settings_at


def host_with(*, services: list[Any] | None = None, shutdown: Any = None, budget: float = 1) -> RuntimeHost:
    return RuntimeHost(
        Mock(
            init_resources=AsyncMock(),
            shutdown_resources=shutdown or AsyncMock(),
            wire=Mock(),
            unwire=Mock(),
        ),
        services or [],
        ApplicationLifetime(),
        HostOptions(shutdown_timeout=budget),
        on_stopping=Mock(),
    )


async def test_services_and_resources_share_one_budget_without_cancelling_finalizers() -> None:
    service_finished, resources_finished = asyncio.Event(), asyncio.Event()
    resource_entered, release = asyncio.Event(), asyncio.Event()
    expired = asyncio.Event()
    service = Mock(start=AsyncMock())

    async def stop(_stopping: asyncio.Event) -> None:
        await asyncio.sleep(0.06)
        service_finished.set()

    async def shutdown() -> None:
        resource_entered.set()
        await release.wait()
        resources_finished.set()

    service.stop = stop
    host = host_with(services=[service], shutdown=shutdown, budget=0.2)
    host._log = Mock()
    host._log.error.side_effect = lambda event, **_kw: (
        expired.set() if event == "host.shutdown.over_budget" else None
    )
    await host.start_async()
    stopping = asyncio.create_task(host.stop_async())
    try:
        await asyncio.wait_for(resource_entered.wait(), 2)
        await asyncio.wait_for(expired.wait(), 2)
        assert service_finished.is_set()
        assert not resources_finished.is_set() and not host.lifetime.stopped.is_set()
        assert host.exit_code == 1
        assert host.shutdown_outcome == ShutdownOutcome.OVER_BUDGET
        assert any(
            call.args == ("host.shutdown.over_budget",) and call.kwargs["phase"] == "resources"
            for call in host._log.error.call_args_list
        )
    finally:
        release.set()
        await stopping
    assert resources_finished.is_set() and host.lifetime.stopped.is_set()
    assert host.exit_code == 1 and host.stop_reason == StopReason.GRACEFUL
    assert host._log.error.call_args.kwargs["shutdown_outcome"] == ShutdownOutcome.OVER_BUDGET


@pytest.mark.parametrize("reason", [StopReason.SIGNAL, StopReason.BACKGROUND_CRASH])
async def test_shutdown_failure_preserves_cause_and_redacts_exception(reason: StopReason) -> None:
    host = host_with(shutdown=AsyncMock(side_effect=RuntimeError("private-connection-secret")))
    host._log = Mock()
    await host.start_async()
    host.lifetime.stop_application(reason)
    await host.stop_async()
    await host.stop_async()
    assert host.stop_reason == reason
    assert host.exit_code == 1 and host.shutdown_outcome == ShutdownOutcome.FAILED
    host.container.shutdown_resources.assert_awaited_once()
    assert "private-connection-secret" not in repr(host._log.mock_calls)


async def test_clean_stop_is_idempotent_and_preserves_signal_exit_zero() -> None:
    host = host_with()
    await host.start_async()
    host.lifetime.stop_application(StopReason.SIGNAL)
    await asyncio.gather(host.stop_async(), host.stop_async())
    await host.stop_async()
    host.container.shutdown_resources.assert_awaited_once()
    assert host.lifetime.stopped.is_set()
    assert host.exit_code == 0 and host.shutdown_outcome == ShutdownOutcome.CLEAN
    assert host.stop_reason == StopReason.SIGNAL


async def test_failed_service_stop_is_not_reported_as_acknowledged() -> None:
    service = Mock(start=AsyncMock(), stop=AsyncMock(side_effect=RuntimeError("stop failed")))
    host = host_with(services=[service])
    await host.start_async()
    await host.stop_async()
    assert host._started_services == [service]
    assert host.exit_code == 1 and host.shutdown_outcome == ShutdownOutcome.FAILED
    host.container.shutdown_resources.assert_awaited_once()


@pytest.mark.parametrize("resource_failure", [False, True])
async def test_startup_rollback_uses_owned_stop_and_retains_initial_failure(
    resource_failure: bool,
) -> None:
    service = Mock(start=AsyncMock(), stop=AsyncMock())
    failing = Mock(start=AsyncMock(side_effect=RuntimeError("startup failed")))
    host = host_with(services=[service, failing])
    if resource_failure:
        host.container.init_resources.side_effect = RuntimeError("startup failed")
    with pytest.raises(RuntimeError, match="startup failed"):
        await host.start_async()
    await host.stop_async()
    assert host.exit_code == 1 and host.lifetime.stopped.is_set()
    assert host.stop_reason == (
        StopReason.RESOURCE_INIT_FAILED if resource_failure else StopReason.STARTUP_FAILED
    )
    host.container.shutdown_resources.assert_awaited_once()
    assert service.stop.await_count == (0 if resource_failure else 1)


@pytest.mark.parametrize("warmup", [False, True])
async def test_opening_stop_reports_deadline_but_retains_owner_until_native_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warmup: bool
) -> None:
    settings = settings_at(tmp_path)
    settings.embedding = EmbeddingSettings(provider="openai", model="fixture")
    init_vector_store(settings.zvec)
    host, provider = build_cli_host(settings)
    host.options.shutdown_timeout = 0.05
    if warmup:
        host.services.append(RuntimeWarmupWorker(provider))
    loop = asyncio.get_running_loop()
    entered, expired = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    opened: list[ZvecChunkSink] = []

    def blocked(_worker: KnowledgeWorker, path: str, **kwargs: Any) -> ZvecChunkSink:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        sink = open_chunk_sink(path, **kwargs)
        opened.append(sink)
        return sink

    monkeypatch.setattr(KnowledgeWorker, "open", blocked)
    monkeypatch.setattr(
        httpx.AsyncClient, "send", AsyncMock(side_effect=AssertionError("No external HTTP allowed."))
    )
    host._log = Mock()
    host._log.error.side_effect = lambda event, **_kw: (
        expired.set() if event == "host.shutdown.over_budget" else None
    )
    await host.start_async()
    runtime = await resolve_runtime(provider)
    opening = None if warmup else asyncio.create_task(runtime.sink())
    stops: list[asyncio.Task[None]] = []
    try:
        await asyncio.wait_for(entered.wait(), 3)
        host.lifetime.stop_application(StopReason.SIGNAL)
        with pytest.raises(RuntimeFailure, match="closing"):
            await runtime.sink()
        stops = [asyncio.create_task(host.stop_async()) for _ in range(2)]
        await asyncio.wait_for(expired.wait(), 3)
        assert runtime.knowledge_index_status()["admission"] == "closing"
        assert not any(task.done() for task in stops)
        assert not host.lifetime.stopped.is_set()
        with pytest.raises(RuntimeFailure), RuntimeLock(settings):
            pytest.fail("The native-opening owner must retain all writer locks.")
        stops[0].cancel()
        await asyncio.sleep(0)
        stops[0].cancel()
        await asyncio.sleep(0)
        assert not any(task.done() for task in stops)
    finally:
        release.set()
        if opening is not None:
            with pytest.raises(RuntimeFailure, match="closing"):
                await opening
        if stops:
            results = await asyncio.gather(*stops, return_exceptions=True)
            assert isinstance(results[0], asyncio.CancelledError)
        await host.stop_async()
    assert len(opened) == 1 and opened[0].admission_snapshot()["admission"] == "closed"
    assert runtime.ready_sink() is None
    assert host.exit_code == 1 and host.stop_reason == StopReason.SIGNAL
    with RuntimeLock(settings):
        pass


async def test_stop_before_executor_dispatch_does_not_open_native_index(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    entered, release = asyncio.Event(), threading.Event()
    native_open = Mock(side_effect=AssertionError("Queued opening must not dispatch after stop."))
    monkeypatch.setattr(runtime._knowledge_worker, "open", native_open)

    def occupy_executor() -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)

    occupied = asyncio.create_task(asyncio.to_thread(occupy_executor))
    opening = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        opening = asyncio.create_task(runtime.sink())
        async with asyncio.timeout(2):
            while runtime.index_open_snapshot() is None:
                await asyncio.sleep(0)
        runtime.begin_shutdown()
    finally:
        release.set()
        await occupied
        if opening is not None:
            with pytest.raises(RuntimeFailure, match="closing"):
                await opening
    native_open.assert_not_called()
    await runtime.close()


async def test_runtime_close_is_shared_across_cancelled_callers(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.memory.close

    async def close_memory() -> None:
        entered.set()
        await release.wait()
        await original()

    close = AsyncMock(side_effect=close_memory)
    monkeypatch.setattr(runtime.memory, "close", close)
    first = asyncio.create_task(runtime.close())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        first.cancel()
        second.cancel()
        await asyncio.sleep(0)
        first.cancel()
        assert not first.done() and not second.done()
    finally:
        release.set()
        results = await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    await runtime.close()
    close.assert_awaited_once()


@pytest.mark.parametrize("trigger", ["signal", "cancel", "frontend", "backend"])
@pytest.mark.parametrize("over_budget", [False, True])
async def test_dev_stops_frontend_during_backend_drain_and_propagates_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trigger: str, over_budget: bool
) -> None:
    backend_draining, frontend_closed, release_backend = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    signal_received, frontend_exit = asyncio.Event(), asyncio.Event()

    async def shutdown() -> None:
        backend_draining.set()
        await release_backend.wait()

    async def process_wait() -> int:
        await frontend_exit.wait()
        return 0

    @asynccontextmanager
    async def frontend(*_args: Any, **_kwargs: Any) -> AsyncIterator[Mock]:
        try:
            yield Mock(wait=process_wait)
        finally:
            frontend_closed.set()

    host = host_with(shutdown=shutdown, budget=0.04 if over_budget else 2)
    monkeypatch.setattr(host, "_register_signal_handlers", lambda: None)
    monkeypatch.setattr(run_module, "_run_frontend", frontend)
    monkeypatch.setattr(run_module, "_wait_for_stop_signal", signal_received.wait)
    supervisor = asyncio.create_task(run_module._supervise(host, tmp_path, 9999))
    try:
        await asyncio.wait_for(host.lifetime.started.wait(), 2)
        if trigger == "cancel":
            supervisor.cancel()
        elif trigger == "frontend":
            frontend_exit.set()
        elif trigger == "backend":
            host.lifetime.stop_application()
        else:
            signal_received.set()
        await asyncio.wait_for(backend_draining.wait(), 2)
        await asyncio.wait_for(frontend_closed.wait(), 2)
        assert not supervisor.done()
        if over_budget:
            await asyncio.sleep(0.06)
    finally:
        release_backend.set()
    assert await supervisor == int(over_budget)
    assert host.shutdown_outcome == (ShutdownOutcome.OVER_BUDGET if over_budget else ShutdownOutcome.CLEAN)


async def test_dev_accounts_for_frontend_cleanup_after_clean_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signal_received, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def frontend(*_args: Any, **_kwargs: Any) -> AsyncIterator[Mock]:
        try:
            yield Mock(wait=asyncio.Event().wait)
        finally:
            await release.wait()

    host = host_with(budget=0.03)
    monkeypatch.setattr(host, "_register_signal_handlers", lambda: None)
    monkeypatch.setattr(run_module, "_run_frontend", frontend)
    monkeypatch.setattr(run_module, "_wait_for_stop_signal", signal_received.wait)
    supervisor = asyncio.create_task(run_module._supervise(host, tmp_path, 9999))
    try:
        await asyncio.wait_for(host.lifetime.started.wait(), 2)
        signal_received.set()
        await asyncio.wait_for(host.lifetime.stopped.wait(), 2)
        await asyncio.sleep(0.05)
        assert not supervisor.done()
    finally:
        release.set()
    assert await supervisor == 1
    assert host.exit_code == 0 and host.shutdown_outcome == ShutdownOutcome.CLEAN


@pytest.mark.parametrize(
    ("installed", "results", "expected_commands", "exit_code"),
    [
        (False, [0, 0], ["ci", "ls"], None),
        (True, [0], ["ls"], None),
        (True, [1, 0, 0], ["ls", "ci", "ls"], None),
        (False, [7], ["ci"], 7),
        (False, [0, 1], ["ci", "ls"], 1),
    ],
)
async def test_dev_repairs_dependencies_using_lock_and_verifies_before_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed: bool,
    results: list[int],
    expected_commands: list[str],
    exit_code: int | None,
) -> None:
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    if installed:
        (tmp_path / "node_modules").mkdir()
    commands: list[str] = []
    codes = iter(results)

    @asynccontextmanager
    async def npm(directory: Path, *args: str, **kwargs: Any) -> AsyncIterator[Mock]:
        assert directory == tmp_path
        commands.append(args[0])
        assert "--include=dev" in args and "--include=optional" in args
        if args[0] == "ls":
            assert kwargs["output"] == asyncio.subprocess.DEVNULL
        yield Mock(wait=AsyncMock(return_value=next(codes)))

    monkeypatch.setattr(run_module, "_run_npm", npm)
    if exit_code is None:
        await run_module._ensure_frontend_dependencies(tmp_path)
    else:
        with pytest.raises(typer.Exit) as error:
            await run_module._ensure_frontend_dependencies(tmp_path)
        assert error.value.exit_code == exit_code
    assert commands == expected_commands


async def test_dev_missing_lock_does_not_run_npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = AsyncMock()
    monkeypatch.setattr(run_module.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(typer.Exit):
        await run_module._ensure_frontend_dependencies(tmp_path)
    spawn.assert_not_awaited()


async def test_dev_dependency_failure_prevents_host_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_module, "_load_settings_or_exit", lambda: settings_at(tmp_path))
    monkeypatch.setattr(run_module, "_resolve_frontend_dir", lambda _override: tmp_path)
    monkeypatch.setattr(
        run_module, "_ensure_frontend_dependencies", AsyncMock(side_effect=typer.Exit(code=7))
    )
    build = Mock()
    monkeypatch.setattr(run_module, "build_host", build)
    with pytest.raises(typer.Exit) as error:
        await run_module._run_dev_logged(tmp_path)
    assert error.value.exit_code == 7
    build.assert_not_called()


async def test_dev_dependency_install_reports_missing_npm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        run_module.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError)
    )
    with pytest.raises(typer.Exit):
        await run_module._ensure_frontend_dependencies(tmp_path)
    captured = capsys.readouterr()
    assert "install Node.js" in captured.out + captured.err


async def test_cancelled_npm_install_terminates_process_tree_and_closes_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    process = Mock(pid=123, returncode=None)
    monkeypatch.setattr(run_module, "_IS_WINDOWS", True)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(run_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(run_module, "create_kill_job", Mock(return_value=456))
    monkeypatch.setattr(run_module, "resume_suspended_process", Mock())
    close_job = Mock()
    monkeypatch.setattr(run_module, "close_kill_job", close_job)

    async def terminate(_process: Any) -> None:
        process.returncode = 1

    cleanup = AsyncMock(side_effect=terminate)
    monkeypatch.setattr(run_module, "_terminate", cleanup)

    async def install() -> None:
        async with run_module._run_npm(tmp_path, "ci", output=asyncio.subprocess.DEVNULL):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(install())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cleanup.assert_awaited_once_with(process)
    close_job.assert_called_once_with(456)
    assert spawn.call_args.args == ("npm.cmd", "ci")
    assert spawn.call_args.kwargs["creationflags"] & 4


@pytest.mark.parametrize("exit_code", [0, 1])
def test_production_interrupt_preserves_shutdown_failure_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    host = Mock(run_async=AsyncMock(), exit_code=exit_code)

    def interrupt(coroutine: Any) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(run_module, "build_host", lambda *_args, **_kw: host)
    monkeypatch.setattr(run_module, "_load_settings_or_exit", lambda: settings_at(tmp_path))
    monkeypatch.setattr(run_module.asyncio, "run", interrupt)
    if exit_code:
        with pytest.raises(typer.Exit) as error:
            run_module.run_prd(frontend_dir=tmp_path)
        assert error.value.exit_code == 1
    else:
        with pytest.raises(KeyboardInterrupt):
            run_module.run_prd(frontend_dir=tmp_path)


async def test_frontend_cleanup_without_process_exit_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = Mock(pid=123, returncode=None)
    monkeypatch.setattr(run_module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(run_module, "create_kill_job", Mock(return_value=456))
    monkeypatch.setattr(run_module, "resume_suspended_process", Mock())
    monkeypatch.setattr(run_module, "close_kill_job", Mock())
    monkeypatch.setattr(run_module, "_terminate", AsyncMock())
    with pytest.raises(RuntimeFailure, match="did not exit"):
        async with run_module._run_frontend(tmp_path, capture_output=False):
            pass


@pytest.mark.parametrize("already_exited", [False, True])
async def test_frontend_job_is_bound_before_execution_and_closed_after_shim_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_exited: bool
) -> None:
    process = Mock(pid=123, returncode=None)
    events: list[str] = []
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(run_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(run_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(
        run_module, "create_kill_job", Mock(side_effect=lambda _pid: events.append("owned") or 456)
    )
    monkeypatch.setattr(
        run_module, "resume_suspended_process", Mock(side_effect=lambda _pid: events.append("resumed"))
    )
    close_job = Mock(side_effect=lambda _job: events.append("closed"))
    monkeypatch.setattr(run_module, "close_kill_job", close_job)

    async def terminate(_process: Any) -> None:
        events.append("terminate")
        process.returncode = 1

    monkeypatch.setattr(run_module, "_terminate", terminate)
    async with run_module._run_frontend(tmp_path, capture_output=False):
        assert events == ["owned", "resumed"]
        if already_exited:
            process.returncode = 1
    assert events == ["owned", "resumed", "terminate", "closed"]
    assert spawn.call_args.kwargs["creationflags"] & 4
    close_job.assert_called_once_with(456)


@pytest.mark.parametrize("stage", ["assignment", "resume"])
async def test_frontend_containment_failure_cleans_up_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    process = Mock(pid=123, returncode=1)
    monkeypatch.setattr(run_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(run_module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(
        run_module,
        "create_kill_job",
        Mock(return_value=456, side_effect=OSError("Containment failed.") if stage == "assignment" else None),
    )
    resume = Mock(side_effect=OSError("Containment failed.") if stage == "resume" else None)
    monkeypatch.setattr(run_module, "resume_suspended_process", resume)
    close_job = Mock()
    monkeypatch.setattr(run_module, "close_kill_job", close_job)
    terminate = AsyncMock()
    monkeypatch.setattr(run_module, "_terminate", terminate)
    with pytest.raises(OSError, match="Containment failed"):
        async with run_module._run_frontend(tmp_path, capture_output=False):
            pytest.fail("An unowned frontend must not be published.")
    terminate.assert_awaited_once_with(process)
    if stage == "assignment":
        resume.assert_not_called()
        close_job.assert_not_called()
    else:
        resume.assert_called_once_with(123)
        close_job.assert_called_once_with(456)


async def web_stop_child(root: Path, development: bool) -> int:
    settings = settings_at(root)
    settings.embedding = EmbeddingSettings(provider="openai", model="fixture")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings.http_port = settings.dev_http_port = port
    directory = root / "static"
    directory.mkdir()
    (directory / "index.html").write_text("<html>Shutdown fixture</html>", encoding="utf-8")
    init_vector_store(settings.zvec)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    opened: list[ZvecChunkSink] = []
    original_send = httpx.AsyncClient.send

    def blocked(_worker: KnowledgeWorker, path: str, **kwargs: Any) -> ZvecChunkSink:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        sink = open_chunk_sink(path, **kwargs)
        opened.append(sink)
        return sink

    async def local_only(client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        assert request.url.host == "127.0.0.1" and request.url.port == port
        return await original_send(client, request, **kwargs)

    KnowledgeWorker.open = blocked
    httpx.AsyncClient.send = local_only
    host = build_host(settings, development=development, frontend=directory)
    host.options.shutdown_timeout = 0.05
    stopping = None
    try:
        await host.start_async()
        await asyncio.wait_for(entered.wait(), 3)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            assert (await client.get("/api/v1/status")).status_code == 200
            assert "Shutdown fixture" in (await client.get("/")).text
        host._on_signal(signal.SIGTERM)
        stopping = asyncio.create_task(host.stop_async())
        await asyncio.sleep(0.12)
        assert not stopping.done() and not host.lifetime.stopped.is_set()
        with pytest.raises(RuntimeFailure), RuntimeLock(settings):
            raise AssertionError("Owner lock released before native drain.")
    finally:
        release.set()
        if stopping is not None:
            await stopping
        await host.stop_async()
    assert len(opened) == 1 and opened[0].admission_snapshot()["admission"] == "closed"
    assert host.shutdown_outcome == ShutdownOutcome.OVER_BUDGET
    assert host.stop_reason == StopReason.SIGNAL and host.exit_code == 1
    assert not endpoint_file(settings).exists()
    (root / "shutdown-result.json").write_text(
        json.dumps({"port": port, "closed_handles": len(opened), "exit_code": host.exit_code}),
        encoding="utf-8",
    )
    return host.exit_code


@pytest.mark.parametrize("development", [False, True])
async def test_real_web_process_exits_after_opening_drain_and_releases_port_and_locks(
    tmp_path: Path, development: bool
) -> None:
    script = (
        "import asyncio,sys; from pathlib import Path; "
        "sys.path.insert(0,str(Path.cwd()/'backend'/'tests')); "
        "from test_runtime_shutdown import web_stop_child; "
        "sys.exit(asyncio.run(web_stop_child(Path(sys.argv[1]),sys.argv[2]=='True')))"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(tmp_path),
        str(development),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    assert process.returncode == 1, (stdout.decode(errors="replace"), stderr.decode(errors="replace"))
    result = json.loads((tmp_path / "shutdown-result.json").read_text(encoding="utf-8"))
    assert result["closed_handles"] == 1 and result["exit_code"] == 1
    with RuntimeLock(settings_at(tmp_path)):
        pass
    with socket.socket() as probe:
        if sys.platform == "win32":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", result["port"]))
        probe.listen()
