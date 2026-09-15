#-----------------------------------------------------------------------------
# 提供shovel run子命令
#
# 用法: shovel run [dev|prd]
# run dev:
# 运行开发环境
#  1. 在项目的前端目录里: frontend 里运行npm run dev, 显示前端
#  2. 在项目的后端目录里: backend 里运行基于py_generic_host的后端应用
# 前端应用和后端应用可以一起运行，方便调试测试和开发
#
# run prd:
# 将前端项目编译后部署到backend项目可以mount的目录里,使用单一的uvicorn 单一运行服务。
#
# 日期: 2026-09-15
# 作者: HongWei Guo <hongweig@163.com>
#------------------------------------------------------------------------------
from __future__ import annotations

import asyncio

import structlog
import typer
from fastapi import FastAPI
from py_generic_host.hosting.builder import HostContext, WebHostBuilder

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.config.settings import AppSettings, load_settings
from shovel.container import AppContainer

app = typer.Typer(
    help="run shovel [dev|prd]",
    invoke_without_command=False,
    no_args_is_help=True
)

@app.command(name="dev")
@command_handler()
def run_dev() -> None:
    """
    Running project in dev mode.
    """
    asyncio.run(_run_dev_async())




@app.command(name="prd")
@command_handler()
def run_prd():
    """
    Running project in production mode.
    """
    console.print("Running Production Environment.")





async def _run_dev_async() -> None:
    console.print("Running dev environment.")

    console.print("Shovel trying to get the config file...")

    config_path = cli_context.get_default_config_file()

    console.success(f"Shovel configuration file path {config_path}")

    if not cli_context.validate_config_exists():
        console.error(
            "Shovel configuration missing, "
            "please use command 'shovel config' to generate a default one."
        )
        raise typer.Exit(code=1)

    settings: AppSettings = load_settings(config_path)

    container = AppContainer()
    container.config.from_dict(settings.model_dump())

    logger = structlog.get_logger(settings.service_name)

    logger.info(
        "shovel boot start",
        env="dev",
    )

    host = (
        WebHostBuilder()
        .use_settings(container.config)
        .use_container(container)
        .use_urls(
            settings.http_host,
            settings.http_port,
        )
        .configure_web_app(_configure_app)
        .build()
    )

    await host.run_async()



def _configure_app(ctx: HostContext, app: FastAPI) -> None:
    pass