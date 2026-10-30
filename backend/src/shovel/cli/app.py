from __future__ import annotations

import os

import typer

from shovel.cli.commands import platform
from shovel.cli.commands.config import app as config_app
from shovel.cli.commands.history import app as history_app
from shovel.cli.commands.init import app as init_app
from shovel.cli.commands.operation import app as operation_app
from shovel.cli.commands.profile import app as profile_app
from shovel.cli.commands.resource import app as resource_app
from shovel.cli.commands.run import app as run_app
from shovel.cli.commands.scan import app as scan_app
from shovel.cli.commands.version import app as version_app
from shovel.cli.context import cli_context

app = typer.Typer(
    name="shovel",
    help="Shovel - Personal AI Agent Platform.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)


@app.callback()
def callback(
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Enable debug output",
    ),
    request_key: str | None = typer.Option(
        None, "--request-key", help="Idempotency key for one write intent; reuse only for the same request."
    ),
) -> None:
    cli_context.request_key = request_key
    if debug:
        os.environ["SHOVEL_DEBUG"] = "1"


# Version 子命令
app.add_typer(
    version_app,
    name="version",
    help="Show Shovel version and runtime information.",
)


# init 初始化命令
app.add_typer(
    init_app,
    name="init",
    help="Shovel will initial all setup.",
)


# config 配置文件管理命令
app.add_typer(
    config_app,
    name="config",
    help="Show, validate or regenerate the configuration file.",
)


# run project命令
app.add_typer(run_app, name="run", help="running project in [dev|prd] model.")


# resource 数据源资源管理命令
app.add_typer(
    resource_app,
    name="resource",
    help="Manage data-source resources.",
)


# scan 扫描与索引命令
app.add_typer(
    scan_app,
    name="scan",
    help="Scan resources and build the search index.",
)
app.add_typer(operation_app, name="operation", help="Inspect and recover durable background operations.")
app.add_typer(profile_app, name="profile", help="Manage persisted scan profiles.")
app.add_typer(history_app, name="history", help="Explicitly prune terminal task history.")
app.add_typer(platform.capability, name="capability", help="Discover registered capabilities.")
app.add_typer(platform.skill, name="skill", help="Inspect, validate and explicitly publish Skills.")
app.add_typer(platform.workflow, name="workflow", help="Validate, publish and run Workflows.")
app.add_typer(platform.proactive, name="proactive", help="Manage Proactive Services.")
app.add_typer(platform.approval, name="approval", help="Review structured Execution approvals.")
app.add_typer(platform.execution, name="execution", help="Inspect and control durable Executions.")
app.add_typer(platform.interaction, name="interaction", help="Inspect and explicitly answer input requests.")
