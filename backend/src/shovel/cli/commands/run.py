# -----------------------------------------------------------------------------
# 提供 shovel run 子命令
#
# 用法: shovel run [dev|prd]
#
# run dev:
#   1. 在项目的 frontend 目录里运行 npm run dev
#   2. 运行基于 py_generic_host 的后端应用
#   两者并发运行, 任一方退出或 Ctrl+C 时整体优雅关闭
#
# run prd:
#   将前端编译产物部署到 backend 可 mount 的目录, 用单一 uvicorn 提供服务
#
# 日期: 2026-09-15
# 作者: HongWei Guo <hongweig@163.com>
# -----------------------------------------------------------------------------
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import AsyncGenerator, Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import structlog
import typer
from rich.text import Text

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.cli.ui.logo import get_logo, get_tagline
from shovel.config.settings import AppSettings, load_settings
from shovel.runtime.hosting import build_host

# -----------------------------------------------------------------------------
# 平台适配层
#
# os.getpgid / os.killpg / signal.SIGKILL 在 typeshed 里被 `sys.platform != "win32"`
# 包着, Pylance 的 pythonPlatform 为 Windows 时会判定它们不存在。
# 这里统一用 getattr 解析一次, 下面的代码只依赖这几个变量, 静态检查在任何平台
# 配置下都不会报错, 运行时行为完全不变。
# -----------------------------------------------------------------------------
_IS_WINDOWS = sys.platform == "win32"

_getpgid: Callable[[int], int] | None = getattr(os, "getpgid", None)
_killpg: Callable[[int, int], None] | None = getattr(os, "killpg", None)
_SIGKILL: int = int(getattr(signal, "SIGKILL", signal.SIGTERM))
_CREATE_NEW_PROCESS_GROUP: int = int(
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
)

_PROJECT_MARKER = "pyproject.toml"
_FRONTEND_DIR_NAME = "frontend"
_TERMINATE_TIMEOUT = 5.0

app = typer.Typer(
    help="run shovel [dev|prd]",
    invoke_without_command=False,
    no_args_is_help=True,
)


#----------------------------------------------------------------------
# 日志输出层
#---------------------------------------------------------------------

_WEB_PREFIX = "web"
_WEB_STYLE = "bold magenta"
_PUMP_DRAIN_TIMEOUT = 2.0



# -----------------------------------------------------------------------------
# 命令入口
# -----------------------------------------------------------------------------

@app.command(name="dev")
@command_handler()
def run_dev(
    frontend_dir: Annotated[
        Path | None,
        typer.Option(
            "--frontend-dir",
            help="前端项目路径, 默认取 <项目根>/frontend",
            file_okay=False,
            dir_okay=True,
        ),
    ] = None,
) -> None:
    """Running project in dev mode."""
    asyncio.run(_run_dev_async(frontend_dir))


@app.command(name="prd")
@command_handler()
def run_prd(
    frontend_dir: Annotated[
        Path | None,
        typer.Option("--frontend-dir", help="生产静态导出目录,默认使用 frontend_path 或 frontend/out。"),
    ] = None,
) -> None:
    """Running project in production mode."""
    settings = _load_settings_or_exit()
    if frontend_dir is None:
        if settings.frontend_path:
            frontend_dir = Path(settings.frontend_path).expanduser()
        else:
            packaged = Path(__file__).resolve().parents[2] / "static"
            frontend_dir = packaged if packaged.is_dir() else _resolve_frontend_dir(None) / "out"
    host = build_host(settings, development=False, frontend=frontend_dir)
    console.info(f"Production → http://{settings.http_host}:{settings.http_port}")
    asyncio.run(host.run_async())
    if host.exit_code:
        raise typer.Exit(code=host.exit_code)


# -----------------------------------------------------------------------------
# dev 主流程
# -----------------------------------------------------------------------------

async def _run_dev_async(frontend_dir_override: Path | None = None) -> None:
    console.print()
    console.print(get_logo())
    console.print(get_tagline())
    console.info("Running dev environment.")

    settings = _load_settings_or_exit()
    frontend_dir = _resolve_frontend_dir(frontend_dir_override)

    logger = structlog.get_logger(settings.service_name).bind(component="api")
    logger.info("shovel boot start", env="dev")

    host = build_host(settings, development=True)

    console.info(f"Frontend → {frontend_dir}  (npm run dev)")
    console.info(f"Backend  → http://{settings.dev_http_host}:{settings.dev_http_port}")

    exit_code = await _supervise(
        host, frontend_dir, settings.dev_http_port,
        backend_host=settings.dev_http_host, frontend_port=settings.dev_frontend_port,
    )
    if exit_code:
        raise typer.Exit(code=exit_code)


async def _supervise(
    host: Any, frontend_dir: Path, backend_port: int,
    *, backend_host: str = "127.0.0.1", frontend_port: int = 3000,
) -> int:
    """并发运行前后端, 返回退出码。任一方结束或收到停止信号即整体关闭。"""
    async with _run_frontend(
        frontend_dir, backend_port, backend_host=backend_host, frontend_port=frontend_port,
    ) as npm_proc:
        backend_task = asyncio.create_task(host.run_async(), name="backend")
        frontend_task = asyncio.create_task(npm_proc.wait(), name="frontend")
        stop_task = asyncio.create_task(_wait_for_stop_signal(), name="stop")
        all_tasks = [backend_task, frontend_task, stop_task]

        try:
            done, pending = await asyncio.wait(
                all_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Windows 上没有 add_signal_handler, Ctrl+C 走这条路
            console.info("Shutting down dev environment...")
            await _cancel_all(all_tasks)
            return 0

        await _cancel_all(pending)

        if stop_task in done:
            console.info("Shutting down dev environment...")
            return 0

        exit_code = 0

        if frontend_task in done:
            rc = frontend_task.result()
            if rc != 0:
                console.error(f"Frontend exited with code {rc}")
                # 被信号杀死时 rc 为负, 归一化成合法退出码
                exit_code = rc if rc > 0 else 1
            else:
                console.info("Frontend dev server stopped.")

        if backend_task in done:
            exc = backend_task.exception()
            if exc is not None:
                console.error(f"Backend crashed: {exc!r}")
                exit_code = exit_code or 1
            else:
                console.info("Backend stopped.")
                exit_code = exit_code or host.exit_code

        return exit_code


async def _cancel_all(tasks: Iterable[asyncio.Task[Any]]) -> None:
    alive = [task for task in tasks if not task.done()]
    for task in alive:
        task.cancel()
    if alive:
        await asyncio.wait(alive)


async def _wait_for_stop_signal() -> None:
    """等待 SIGINT / SIGTERM。平台不支持时永久挂起, 由 KeyboardInterrupt 兜底。"""
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    registered: list[signal.Signals] = []

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError, AttributeError):
            continue
        registered.append(sig)

    try:
        await stop_event.wait()
    finally:
        for sig in registered:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)


# -----------------------------------------------------------------------------
# 配置与路径
# -----------------------------------------------------------------------------

def _load_settings_or_exit() -> AppSettings:
    config_path = cli_context.get_default_config_file()

    if not cli_context.validate_config_exists():
        console.error(
            "Shovel configuration missing, "
            "please use command 'shovel init' to generate a default one."
        )
        raise typer.Exit(code=1)

    console.success(f"Shovel configuration file path {config_path}")

    with console.status("[cyan]Loading Shovel configuration..."):
        return load_settings(config_path)


def _resolve_frontend_dir(override: Path | None) -> Path:
    if override is not None:
        frontend_dir = override.expanduser().resolve()
    else:
        project_root = _find_project_root()
        if project_root is None:
            console.error(
                f"Not inside a Shovel project "
                f"(no {_PROJECT_MARKER} found from current directory upwards)."
            )
            raise typer.Exit(code=1)
        frontend_dir = project_root / _FRONTEND_DIR_NAME

    if not (frontend_dir / "package.json").exists():
        console.error(f"Frontend project not found at {frontend_dir}")
        raise typer.Exit(code=1)

    return frontend_dir


def _find_project_root(start: Path | None = None) -> Path | None:
    """向上查找项目根。优先匹配同时含 pyproject.toml 与 frontend/ 的目录。"""
    current = (start or Path.cwd()).resolve()
    candidates = (current, *current.parents)

    for candidate in candidates:
        if (candidate / _PROJECT_MARKER).exists() and (
            candidate / _FRONTEND_DIR_NAME
        ).is_dir():
            return candidate

    for candidate in candidates:          # 回退: 只认 marker
        if (candidate / _PROJECT_MARKER).exists():
            return candidate

    return None


# -----------------------------------------------------------------------------
# 前端子进程
# -----------------------------------------------------------------------------

def _npm_cmd() -> str:
    # Windows 上 npm 是 npm.cmd, 不带后缀 create_subprocess_exec 会 FileNotFoundError
    return "npm.cmd" if _IS_WINDOWS else "npm"


@asynccontextmanager
async def _run_frontend(
    frontend_dir: Path,
    backend_port: int | None = None,
    capture_output: bool = True,
    *,
    backend_host: str = "127.0.0.1",
    frontend_port: int = 3000,
) -> AsyncGenerator[asyncio.subprocess.Process]:
    """启动 npm run dev, 退出上下文时保证整棵子进程树被关闭。"""
    kwargs: dict[str, Any] = {}
    if _IS_WINDOWS:
        kwargs["creationflags"] = _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    env = dict(os.environ)
    # 保留开发服务器通过管道输出时的颜色。
    env["FORCE_COLOR"] = "1"
    if backend_port is not None:
        env["SHOVEL_BACKEND_PORT"] = str(backend_port)
    env["SHOVEL_BACKEND_HOST"] = backend_host

    if capture_output:
        kwargs["stdout"] = asyncio.subprocess.PIPE
        # 合并到 stdout, 用一个泵保证行间顺序
        kwargs["stderr"] = asyncio.subprocess.STDOUT

    try:
        proc = await asyncio.create_subprocess_exec(
            _npm_cmd(),
            "run",
            "dev",
            "--",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(frontend_port),
            cwd=str(frontend_dir),
            env=env,
            **kwargs,
        )
    except FileNotFoundError as exc:
        console.error("`npm` not found on PATH. Please install Node.js first.")
        raise typer.Exit(code=1) from exc

    pumps: list[asyncio.Task[Any]] = []
    if capture_output:
        pumps.append(
            asyncio.create_task(
                _pump_stream(proc.stdout, _WEB_PREFIX, _WEB_STYLE),
                name="pump-web",
            )
        )

    try:
        yield proc
    finally:
        await _terminate(proc)
        if pumps:
            # 给泵一点时间把最后几行冲出来, 超时就砍掉
            _, still_running = await asyncio.wait(
                pumps, timeout=_PUMP_DRAIN_TIMEOUT,
            )
            await _cancel_all(still_running)


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return

    if _IS_WINDOWS:
        await _terminate_windows(proc)
    else:
        await _terminate_posix(proc)


async def _terminate_windows(proc: asyncio.subprocess.Process) -> None:
    """npm.cmd 只是 shim, TerminateProcess 杀不到真正跑 vite 的 node 进程,
    必须用 taskkill /T 按进程树关闭, 否则端口会被孤儿进程一直占着。"""
    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/F",
            "/T",
            "/PID",
            str(proc.pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    except (FileNotFoundError, OSError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_TIMEOUT)


async def _terminate_posix(proc: asyncio.subprocess.Process) -> None:
    """POSIX 下按进程组关闭 npm 及其所有子进程。"""
    if _getpgid is None or _killpg is None:      # Windows, 运行时不会走到
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return

    try:
        pgid = _getpgid(proc.pid)
    except ProcessLookupError:
        return

    with contextlib.suppress(ProcessLookupError, PermissionError):
        _killpg(pgid, signal.SIGTERM)

    try:
        await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            _killpg(pgid, _SIGKILL)
        await proc.wait()


async def _pump_stream(
    stream: asyncio.StreamReader | None,
    prefix: str,
    style: str,
) -> None:
    """逐行读取子进程输出, 加前缀后转发到 console。"""
    if stream is None:
        return

    while True:
        try:
            raw = await stream.readline()
        except (asyncio.LimitOverrunError, ValueError):
            # 单行超过 StreamReader 默认 64 KiB 上限, 跳过这一段继续读
            continue
        except asyncio.CancelledError:
            raise

        if not raw:                  # EOF
            break

        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not text.strip():
            continue

        # Text.from_ansi 把 vite 的 ANSI 颜色转成 rich 的样式,
        # 同时避免 vite 输出里的 "[" 被 rich 当成 markup 解析
        console.print(
            Text.assemble((f"{prefix:>4} ", style), Text.from_ansi(text)),
            highlight=False,
        )
