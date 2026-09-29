#---------------------------------------------------------
# shovel resource 子命令
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------
"""资源的命令行入口。

## 关于密钥输入

密钥**只**从 stdin 或隐藏输入读, 不接受命令行参数。
``--secret xxx`` 看起来方便, 代价是这个密钥会同时出现在 shell history、
``ps`` 的输出、以及部分系统的审计日志里 —— 三个用户完全意识不到的地方。
这个便利不值得。
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer
from rich.box import SIMPLE
from rich.table import Table

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.config.settings import load_settings
from shovel.db.database import (
    create_async_database_engine,
    create_async_session_factory,
)
from shovel.db.model.config import Resource
from shovel.domain.enums import ResourceState, Sensitivity
from shovel.exceptions.resource import InvalidConnectorConfigError
from shovel.services import connector_registry
from shovel.services.resource_service import ResourceDraft, ResourceService

app = typer.Typer(
    help="管理数据源资源 (Resource)。",
    no_args_is_help=True,
)

#: 状态 -> rich 颜色。让 `resource list` 一眼能看出哪些资源是健康的。
_STATE_STYLE: dict[ResourceState, str] = {
    ResourceState.DRAFT: "dim",
    ResourceState.VERIFYING: "yellow",
    ResourceState.ACTIVE: "green",
    ResourceState.DEGRADED: "yellow",
    ResourceState.PAUSED: "cyan",
    ResourceState.REVOKED: "red",
}


# --------------------------------------------------------------------------- #
# 运行时装配
# --------------------------------------------------------------------------- #
def _run[T](factory: Callable[[ResourceService], Coroutine[Any, Any, T]]) -> T:
    """开一个事件循环, 把 service 装好, 跑完再拆掉。

    CLI 的一条命令 = 一次完整的业务操作 = 一个数据库连接的生命周期。
    不复用连接, 因为进程马上就要退出了, 复用的收益是零而复杂度不是。
    """

    async def main() -> T:
        settings = load_settings(cli_context.get_default_config_file())

        engine_ctx = create_async_database_engine(
            settings.database.url,
            echo=settings.database.echo,
        )

        # create_async_database_engine 是 async generator (给 DI 容器用的),
        # 在 CLI 里手工推进它比起把整个 AppContainer 拉起来要轻得多。
        engine = await anext(engine_ctx)

        try:
            service = ResourceService(create_async_session_factory(engine))
            return await factory(service)
        finally:
            await engine_ctx.aclose()

    return asyncio.run(main())


def _read_secret(from_stdin: bool, prompt: bool) -> str | None:
    """从 stdin 或隐藏输入读密钥。命令行参数不是选项 —— 见模块文档。"""

    if from_stdin:
        secret = sys.stdin.read().strip()
        if not secret:
            raise InvalidConnectorConfigError(
                "(secret)", "从 stdin 读到的密钥是空的。"
            )
        return secret

    if prompt:
        secret_input: str = typer.prompt("Secret", hide_input=True)
        return secret_input

    return None


def _parse_config(raw: str | None, pairs: list[str] | None) -> dict[str, Any]:
    """把 ``--config-json`` 与若干 ``--set k=v`` 合成一个 config dict。

    两种入口并存是因为它们服务于不同场景: ``--set root_path=D:/notes``
    适合手敲, ``--config-json`` 适合脚本和嵌套结构。``--set`` 后写后生效。
    """

    config: dict[str, Any] = {}

    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvalidConnectorConfigError(
                "(config)", f"--config-json 不是合法 JSON: {exc}"
            ) from exc

        if not isinstance(parsed, dict):
            raise InvalidConnectorConfigError(
                "(config)", "--config-json 必须是一个 JSON 对象。"
            )

        config.update(parsed)

    for pair in pairs or []:
        if "=" not in pair:
            raise InvalidConnectorConfigError(
                "(config)", f"--set 的格式是 key=value, 收到的是 {pair!r}。"
            )
        key, _, value = pair.partition("=")
        config[key.strip()] = _coerce(value.strip())

    return config


def _coerce(value: str) -> Any:
    """把 ``--set`` 的字符串值转成合适的类型。

    只认 JSON 字面量 (true / 123 / "x" / [1,2]), 其余原样当字符串。
    这样 ``--set follow_symlinks=true`` 得到的是布尔值而不是字符串 "true",
    而 ``--set root_path=D:/notes`` 不会因为不是合法 JSON 就报错。
    """

    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
@app.command("connectors")
@command_handler()
def connectors(
    kind: Annotated[
        str | None,
        typer.Option("--kind", "-k", help="只看某一个连接器的详情。"),
    ] = None,
) -> None:
    """列出这个版本支持的连接器。"""

    if kind is not None:
        _show_connector(kind)
        return

    table = Table(
        title="可用的连接器",
        box=SIMPLE,
        border_style="bright_blue",
        padding=(0, 2),
    )
    table.add_column("Kind", style="bold bright_cyan", no_wrap=True)
    table.add_column("名称", style="white")
    table.add_column("能力", style="dim")
    table.add_column("需要凭据", justify="center")

    for spec in connector_registry.all_specs():
        table.add_row(
            spec.kind,
            spec.display_name,
            ", ".join(sorted(c.value for c in spec.capabilities)),
            "是" if spec.requires_identity else "否",
        )

    console.print(table)


def _show_connector(kind: str) -> None:
    spec = connector_registry.get_spec(kind)

    table = Table(
        title=f"连接器 {spec.kind}",
        box=SIMPLE,
        show_header=False,
        border_style="bright_blue",
        padding=(0, 2),
    )
    table.add_column(style="bold bright_cyan", no_wrap=True)
    table.add_column(style="white")

    table.add_row("名称", spec.display_name)
    table.add_row("说明", spec.description)
    table.add_row("能力", ", ".join(sorted(c.value for c in spec.capabilities)))
    table.add_row("扫描模式", ", ".join(sorted(m.value for m in spec.supported_modes())))
    table.add_row("需要凭据", "是" if spec.requires_identity else "否")
    table.add_row("默认权威度", f"{spec.default_authority:.2f}")
    table.add_row("默认敏感度", spec.default_sensitivity.value)

    console.print(table)

    fields = Table(
        title="配置字段",
        box=SIMPLE,
        border_style="bright_blue",
        padding=(0, 2),
    )
    fields.add_column("字段", style="bold bright_cyan", no_wrap=True)
    fields.add_column("必填", justify="center")
    fields.add_column("默认值", style="dim")
    fields.add_column("说明", style="white")

    for name, info in spec.config_model.model_fields.items():
        fields.add_row(
            name,
            "是" if info.is_required() else "否",
            "—" if info.is_required() else str(info.default),
            info.description or "",
        )

    console.print(fields)


@app.command("add")
@command_handler()
def add(
    name: Annotated[str, typer.Argument(help="资源名, 全局唯一。")],
    kind: Annotated[
        str,
        typer.Option("--kind", "-k", help="连接器类型, 见 `shovel resource connectors`。"),
    ],
    config_json: Annotated[
        str | None,
        typer.Option("--config-json", help="连接配置, 一个 JSON 对象。"),
    ] = None,
    set_values: Annotated[
        list[str] | None,
        typer.Option("--set", "-s", help="以 key=value 的形式设置配置项, 可重复。"),
    ] = None,
    description: Annotated[
        str | None,
        typer.Option("--description", "-d", help="一句话描述, 会展示给 agent。"),
    ] = None,
    sensitivity: Annotated[
        Sensitivity | None,
        typer.Option("--sensitivity", help="敏感度标签。"),
    ] = None,
    authority: Annotated[
        float | None,
        typer.Option("--authority", min=0.0, max=1.0, help="检索排序时的权威度权重。"),
    ] = None,
    identity_ref: Annotated[
        str | None,
        typer.Option(
            "--identity-ref",
            help="凭据引用, 例如 env:MY_TOKEN。不传则在需要时写进系统凭据库。",
        ),
    ] = None,
    secret_stdin: Annotated[
        bool,
        typer.Option("--secret-stdin", help="从 stdin 读取密钥。"),
    ] = False,
    ask_secret: Annotated[
        bool,
        typer.Option("--ask-secret", help="交互式输入密钥 (输入不回显)。"),
    ] = False,
    verify_now: Annotated[
        bool,
        typer.Option("--verify/--no-verify", help="创建后立刻校验一次。"),
    ] = True,
) -> None:
    """新建一个资源。"""

    from pydantic import SecretStr

    config = _parse_config(config_json, set_values)
    raw_secret = _read_secret(secret_stdin, ask_secret)

    draft = ResourceDraft(
        name=name,
        connector_kind=kind,
        config=config,
        description=description,
        sensitivity=sensitivity,
        authority=authority,
        secret=SecretStr(raw_secret) if raw_secret else None,
        identity_ref=identity_ref,
    )

    resource = _run(lambda svc: svc.create(draft))

    console.success(f"资源 '{resource.name}' 已创建 (id={resource.id}, 状态={resource.state.value})。")

    if verify_now:
        result = _run(lambda svc: svc.verify(resource.id))
        _report_verify(resource.name, result)
    else:
        console.info(f"尚未校验。运行 `shovel resource verify {resource.name}` 来确认它是通的。")


@app.command("list")
@command_handler()
def list_resources(
    state: Annotated[
        ResourceState | None,
        typer.Option("--state", help="只看某个状态的资源。"),
    ] = None,
    kind: Annotated[
        str | None,
        typer.Option("--kind", "-k", help="只看某个连接器类型的资源。"),
    ] = None,
) -> None:
    """列出已配置的资源。"""

    resources = _run(lambda svc: svc.list(state=state, connector_kind=kind))

    if not resources:
        console.info("还没有任何资源。用 `shovel resource add` 创建一个。")
        return

    table = Table(
        title="资源",
        box=SIMPLE,
        border_style="bright_blue",
        padding=(0, 2),
    )
    table.add_column("名称", style="bold bright_cyan", no_wrap=True)
    table.add_column("连接器", style="white")
    table.add_column("状态")
    table.add_column("敏感度", style="dim")
    table.add_column("权威度", justify="right", style="dim")
    table.add_column("最后校验", style="dim")

    for item in resources:
        style = _STATE_STYLE.get(item.state, "white")
        table.add_row(
            item.name,
            item.connector_kind,
            f"[{style}]{item.state.value}[/{style}]",
            item.sensitivity.value,
            f"{item.authority:.2f}",
            _format_ts(item.last_verified_at),
        )

    console.print(table)


@app.command("show")
@command_handler()
def show(
    ref: Annotated[str, typer.Argument(help="资源名或 id。")],
) -> None:
    """查看一个资源的详情。"""

    resource = _run(lambda svc: svc.resolve(ref))
    _render_detail(resource)


@app.command("verify")
@command_handler()
def verify(
    ref: Annotated[str, typer.Argument(help="资源名或 id。")],
) -> None:
    """校验一个资源的连通性, 并据此更新它的状态。"""

    with console.status(f"[cyan]正在校验 {ref} ……"):
        result = _run(lambda svc: svc.verify(ref))

    _report_verify(ref, result)


@app.command("pause")
@command_handler()
def pause(
    ref: Annotated[str, typer.Argument(help="资源名或 id。")],
    reason: Annotated[
        str | None,
        typer.Option("--reason", "-r", help="暂停原因, 会记进 state_reason。"),
    ] = None,
) -> None:
    """暂停一个资源, 让调度器不再扫描它。"""

    resource = _run(lambda svc: svc.pause(ref, reason))
    console.success(f"资源 '{resource.name}' 已暂停。")


@app.command("resume")
@command_handler()
def resume(
    ref: Annotated[str, typer.Argument(help="资源名或 id。")],
) -> None:
    """恢复一个被暂停的资源。"""

    resource = _run(lambda svc: svc.resume(ref))
    console.success(f"资源 '{resource.name}' 已恢复, 状态={resource.state.value}。")


@app.command("revoke")
@command_handler()
def revoke(
    ref: Annotated[str, typer.Argument(help="资源名或 id。")],
    reason: Annotated[
        str | None,
        typer.Option("--reason", "-r", help="吊销原因。"),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="跳过确认。"),
    ] = False,
) -> None:
    """吊销一个资源并销毁它的凭据。这是终态, 无法撤销。"""

    if not yes:
        typer.confirm(
            f"确定要吊销 '{ref}' 吗? 它的凭据会被删除, 且状态无法回退。",
            abort=True,
        )

    resource = _run(lambda svc: svc.revoke(ref, reason))
    console.success(f"资源 '{resource.name}' 已吊销, 凭据已销毁。")
    console.info("已索引的文档仍然保留。要一并清除请使用 `shovel resource remove`。")


@app.command("remove")
@command_handler()
def remove(
    ref: Annotated[str, typer.Argument(help="资源名或 id。")],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="跳过确认。"),
    ] = False,
) -> None:
    """删除一个资源, 连同它的扫描配置与调度。"""

    if not yes:
        typer.confirm(
            f"确定要删除 '{ref}' 吗? 它的扫描配置与调度会一并消失。",
            abort=True,
        )

    resource_id = _run(lambda svc: svc.delete(ref))
    console.success(f"资源已删除 (id={resource_id})。")


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def _render_detail(resource: Resource) -> None:
    style = _STATE_STYLE.get(resource.state, "white")

    table = Table(
        title=f"资源 {resource.name}",
        box=SIMPLE,
        show_header=False,
        border_style="bright_blue",
        padding=(0, 2),
    )
    table.add_column(style="bold bright_cyan", no_wrap=True, min_width=14)
    table.add_column(style="white")

    table.add_row("Id", resource.id)
    table.add_row("连接器", resource.connector_kind)
    table.add_row("状态", f"[{style}]{resource.state.value}[/{style}]")
    table.add_row("状态说明", resource.state_reason or "—")
    table.add_row("敏感度", resource.sensitivity.value)
    table.add_row("权威度", f"{resource.authority:.2f}")
    table.add_row("描述", resource.description or "—")
    # 只显示引用, 永远不显示值
    table.add_row("凭据引用", resource.identity_ref or "—")
    table.add_row("最后校验", _format_ts(resource.last_verified_at))
    table.add_row("创建于", _format_ts(resource.created_at))
    table.add_row("更新于", _format_ts(resource.updated_at))

    console.print(table)
    console.print("[bold bright_cyan]连接配置[/]")
    console.print_json(json.dumps(resource.config, ensure_ascii=False))


def _report_verify(ref: str, result: Any) -> None:
    latency = f" ({result.latency_ms} ms)" if result.latency_ms is not None else ""

    if result.ok:
        console.success(f"{ref}: {result.message}{latency}")
    else:
        console.error(f"{ref}: {result.message}{latency}")
        console.info("资源已被标记为 degraded。修正配置后重新运行 `shovel resource verify`。")


def _format_ts(value: int | None) -> str:
    if not value:
        return "—"

    from datetime import UTC, datetime

    return datetime.fromtimestamp(value, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")
