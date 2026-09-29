#---------------------------------------------------------
# shovel scan 子命令
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""扫描的命令行入口。

## 关于 --no-embed

这个开关的存在是因为 embedding 是整条链路上唯一需要外部服务的一步。
没配 API key 就什么都不能做, 会让"我先把本地笔记索引起来看看"变成
一件必须先注册账号的事。加上它之后, chunk 照常写进 SQLite 并停在
``vector_state=pending``, 等 embedding 配好了再补跑一次就能补齐。

## 关于 --limit

只截断"本轮处理多少篇", 不截断 discover。看起来多余 —— 既然只想处理
10 篇, 为什么还要把两万个文件都列一遍? 因为删除对账依赖完整的观测集:
列不全就不敢判定"这个文件消失了", 否则一次 ``--limit 10`` 会把剩下的
19990 篇集体立碑。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer
from rich.box import SIMPLE
from rich.table import Table
from sqlalchemy import desc, select

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.config.settings import AppSettings, load_settings
from shovel.db.database import (
    create_async_database_engine,
    create_async_session_factory,
)
from shovel.db.model.config import Resource
from shovel.db.model.execution import Job, JobEvent
from shovel.domain.enums import JobState, ScanMode
from shovel.pipeline.embedder import build_embedder
from shovel.pipeline.scan_service import ScanOutcome, ScanService
from shovel.pipeline.sink import NullVectorSink, open_chunk_sink
from shovel.services.resource_service import ResourceService

app = typer.Typer(
    help="扫描数据源, 把内容切块并写入索引。",
    no_args_is_help=True,
)

#: 作业状态 -> rich 颜色。`scan list` 要能一眼看出哪几次跑挂了。
_JOB_STYLE: dict[JobState, str] = {
    JobState.QUEUED: "dim",
    JobState.RUNNING: "cyan",
    JobState.SUCCEEDED: "green",
    JobState.COMPLETED_WITH_ERRORS: "yellow",
    JobState.FAILED: "red",
    JobState.CANCELLED: "dim",
}


# --------------------------------------------------------------------------- #
# 运行时装配
# --------------------------------------------------------------------------- #
def _run[T](factory: Callable[[Any, AppSettings], Coroutine[Any, Any, T]]) -> T:
    """开一个事件循环, 把 session factory 装好, 跑完再拆掉。

    和 ``resource`` 子命令保持同一套做法: 一条命令 = 一次完整的业务
    操作 = 一个连接的生命周期。进程马上就要退出, 复用连接的收益是零。
    """

    async def main() -> T:
        settings = load_settings(cli_context.get_default_config_file())

        engine_ctx = create_async_database_engine(
            settings.database.url,
            echo=settings.database.echo,
        )
        engine = await anext(engine_ctx)

        try:
            return await factory(create_async_session_factory(engine), settings)
        finally:
            await engine_ctx.aclose()

    return asyncio.run(main())


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
@app.command("run")
@command_handler()
def run(
    ref: Annotated[str, typer.Argument(help="资源名或 id。")],
    mode: Annotated[
        ScanMode | None,
        typer.Option("--mode", "-m", help="扫描模式。不传则用 profile 上配的。"),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option("--profile", "-p", help="扫描配置名, 默认用 default。"),
    ] = None,
    target: Annotated[
        list[str] | None,
        typer.Option("--target", "-t", help="只扫这几个条目 (可重复)。会强制 targeted 模式。"),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-n", min=1, help="本轮最多处理多少篇。不影响 discover。"),
    ] = None,
    embed: Annotated[
        bool,
        typer.Option("--embed/--no-embed", help="是否生成向量。关掉时 chunk 停在 pending。"),
    ] = True,
) -> None:
    """扫描一个资源。"""

    async def go(sessions: Any, settings: AppSettings) -> ScanOutcome:
        embedder = build_embedder(
            settings.embedding,
            expected_dimension=settings.zvec.dim,
            enabled=embed,
        )

        # 不 embedding 就没有向量可写, 此时去打开 collection 只会在
        # 用户还没 `shovel init` 过的机器上凭空失败一次。
        sink = open_chunk_sink(settings.zvec.knowledge_path) if embed else NullVectorSink()

        service = ScanService(
            sessions,
            ResourceService(sessions),
            embedder=embedder,
            sink=sink,
        )

        try:
            return await service.scan(
                ref,
                mode=mode,
                profile_name=profile,
                target_ids=tuple(target or ()),
                limit=limit,
            )
        finally:
            await embedder.aclose()
            await sink.aclose()

    with console.status(f"[cyan]正在扫描 {ref} ……"):
        outcome = _run(go)

    _report(outcome)


@app.command("list")
@command_handler()
def list_jobs(
    ref: Annotated[
        str | None,
        typer.Option("--resource", "-r", help="只看某个资源的作业。"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", min=1, help="最多显示多少条。"),
    ] = 20,
) -> None:
    """列出最近的扫描作业。"""

    async def go(sessions: Any, _settings: AppSettings) -> list[tuple[Job, str]]:
        stmt = (
            select(Job, Resource.name)
            .join(Resource, Resource.id == Job.resource_id)
            .order_by(desc(Job.started_at))
            .limit(limit)
        )

        if ref:
            resource = await ResourceService(sessions).resolve(ref)
            stmt = stmt.where(Job.resource_id == resource.id)

        async with sessions() as session:
            return [(job, name) for job, name in (await session.execute(stmt)).all()]

    rows = _run(go)

    if not rows:
        console.info("还没有任何扫描作业。用 `shovel scan run <资源>` 跑一次。")
        return

    table = Table(title="扫描作业", box=SIMPLE, border_style="bright_blue", padding=(0, 2))
    table.add_column("Job", style="bold bright_cyan", no_wrap=True)
    table.add_column("资源", style="white")
    table.add_column("模式", style="dim")
    table.add_column("状态")
    table.add_column("发现", justify="right", style="dim")
    table.add_column("索引", justify="right", style="dim")
    table.add_column("失败", justify="right")
    table.add_column("开始于", style="dim")

    for job, name in rows:
        style = _JOB_STYLE.get(job.state, "white")
        table.add_row(
            job.id,
            name,
            job.scan_mode.value,
            f"[{style}]{job.state.value}[/{style}]",
            str(job.discovered),
            str(job.chunks_written),
            f"[red]{job.failed_docs}[/red]" if job.failed_docs else "0",
            _format_ts(job.started_at),
        )

    console.print(table)


@app.command("show")
@command_handler()
def show(
    job_id: Annotated[str, typer.Argument(help="作业 id, 见 `shovel scan list`。")],
    events: Annotated[
        int,
        typer.Option("--events", "-e", min=0, help="显示最近多少条事件。"),
    ] = 20,
) -> None:
    """查看一次扫描的详情与事件日志。"""

    async def go(sessions: Any, _settings: AppSettings) -> tuple[Job | None, list[JobEvent]]:
        async with sessions() as session:
            job = await session.get(Job, job_id)

            if job is None or events == 0:
                return job, []

            stmt = (
                select(JobEvent)
                .where(JobEvent.job_id == job_id)
                .order_by(desc(JobEvent.logged_at))
                .limit(events)
            )

            return job, list((await session.execute(stmt)).scalars().all())

    job, log = _run(go)

    if job is None:
        console.error(f"没有 id 为 {job_id} 的作业。")
        raise typer.Exit(code=1)

    _render_job(job)

    if log:
        _render_events(log)


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def _report(outcome: ScanOutcome) -> None:
    summary = (
        f"发现 {outcome.discovered} 篇, "
        f"索引 {outcome.parsed} 篇, "
        f"跳过 {outcome.skipped_unchanged} 篇, "
        f"写入 {outcome.chunks_written} 个块 / {outcome.vectors_written} 个向量"
    )

    if outcome.state is JobState.SUCCEEDED:
        console.success(f"{outcome.resource_name}: {summary}。")
    elif outcome.state is JobState.COMPLETED_WITH_ERRORS:
        console.warning(f"{outcome.resource_name}: {summary}, 但有 {outcome.failed_docs} 篇失败。")
        console.info(f"用 `shovel scan show {outcome.job_id}` 看是哪几篇。")
    else:
        console.error(f"{outcome.resource_name}: 扫描 {outcome.state.value} —— {outcome.error_detail}")

    if outcome.tombstoned:
        console.info(f"{outcome.tombstoned} 个条目在源端已不存在, 已标记为墓碑。")

    for warning in outcome.warnings[:5]:
        console.warning(warning)


def _render_job(job: Job) -> None:
    style = _JOB_STYLE.get(job.state, "white")

    table = Table(
        title=f"作业 {job.id}",
        box=SIMPLE,
        show_header=False,
        border_style="bright_blue",
        padding=(0, 2),
    )
    table.add_column(style="bold bright_cyan", no_wrap=True, min_width=14)
    table.add_column(style="white")

    table.add_row("状态", f"[{style}]{job.state.value}[/{style}]")
    table.add_row("模式", job.scan_mode.value)
    table.add_row("开始于", _format_ts(job.started_at))
    table.add_row("结束于", _format_ts(job.finished_at))
    # discover 是否跑完决定了这一轮敢不敢做删除对账
    table.add_row("枚举完整", "是" if job.is_discovery_complete else "否")
    table.add_row("发现 / 跳过", f"{job.discovered} / {job.skipped_unchanged}")
    table.add_row("解析 / 切块", f"{job.parsed} / {job.chunked}")
    table.add_row("写入块 / 向量", f"{job.chunks_written} / {(job.stats or {}).get('vectors_written', 0)}")
    table.add_row("失败篇数", str(job.failed_docs))
    table.add_row("读取字节", f"{job.bytes_read:,}")
    table.add_row("错误类型", job.error_kind.value if job.error_kind else "—")
    table.add_row("错误详情", job.error_detail or "—")

    console.print(table)


def _render_events(log: list[JobEvent]) -> None:
    table = Table(title="事件", box=SIMPLE, border_style="bright_blue", padding=(0, 2))
    table.add_column("时间", style="dim", no_wrap=True)
    table.add_column("级别", no_wrap=True)
    table.add_column("代码", style="bold bright_cyan", no_wrap=True)
    # `scan run` 的提示语是"看是哪几篇", 不显示文档就兑现不了这句话
    table.add_column("文档", style="white")
    table.add_column("说明", style="white")

    level_style = {"error": "red", "warn": "yellow", "info": "dim"}

    for event in reversed(log):
        style = level_style.get(event.level.value, "white")
        table.add_row(
            _format_ts(event.logged_at),
            f"[{style}]{event.level.value}[/{style}]",
            event.code or "—",
            _short_uri(event.doc_uri),
            event.message or "",
        )

    console.print(table)


def _short_uri(uri: str | None) -> str:
    """只留最后两段。完整 URI 会把表格撑爆, 而用户认的是文件名。"""

    if not uri:
        return "—"

    parts = uri.replace("\\", "/").rstrip("/").split("/")

    return "/".join(parts[-2:]) if len(parts) > 1 else uri


def _format_ts(value: int | None) -> str:
    if not value:
        return "—"

    from datetime import UTC, datetime

    return datetime.fromtimestamp(value, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")
