from __future__ import annotations

import time
from pathlib import Path

import typer

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console

app = typer.Typer(
    help="Initial shovel agent",
    invoke_without_command=True,
    no_args_is_help=False
)


@app.callback()
@command_handler()
def init() -> None:

    console.info("Begin initial Shovel Agent.")

    #check profile 文件目录
    console.info("Checking the default directories.")

    with console.status(
        "[cyan]Checking the profile path......",
    ):
        time.sleep(2)

    profile_path = cli_context.get_default_profile_dir()

    if Path.exists(profile_path):
        console.success(f"The default profile directory {profile_path} is exists.")
    else:
        console.warning(f"The default profile directory {profile_path} is not eixts. We will create it.")
        Path.mkdir(profile_path)
        console.success(f"Profile directory {profile_path} was created.")

    # check config 文件目录
    with console.status(
        "[cyan]Checking the default configuratrion directory ......",
    ):
        time.sleep(2)

    config_dir = cli_context.get_default_config_dir()

    if Path.exists(config_dir):
        console.success(f"The default configuration directory {config_dir} is exists.")
    else:
        console.warning(f"The default configuration directory {config_dir} is not exists. We will create it.")
        Path.mkdir(config_dir)
        console.success(f"Configuration directory {config_dir} was created.")

    # 检查workspace 目录
    with console.status(
        "[cyan]Checking the default workspace ......",
    ):
        time.sleep(2)

    workspace_dir = cli_context.get_default_workspace()

    if Path.exists(workspace_dir):
        console.success(f"The Default worksapce directory {workspace_dir} is exists.")
    else:
        console.warning(f"The Default worksapce directory {workspace_dir} is not exists. We will create it.")
        workspace_dir.mkdir(parents=True, exist_ok=True)
        console.success(f"Default worksapce directory {workspace_dir} was created.")


    # 检查db 目录(我们这里暂时采用sqlite 来管理运行部分需要数据库的地方)
    with console.status(
        "[cyan]Checking the database directory ......",
    ):
        time.sleep(2)

    db_dir = cli_context.get_default_database_dir()

    if Path.exists(db_dir):
        console.success(f"Database directory {db_dir} is exists.")
    else:
        console.success(f"Database directory {db_dir} is not exists. We will create it.")
        db_dir.mkdir()
        console.success(f"Database directory {db_dir} was created.")


    # 检查 日志目录
    with console.status(
        "[cyan]Checking the log directory ......",
    ):
        time.sleep(2)

    log_dir = cli_context.get_default_logs_dir()

    if Path.exists(log_dir):
        console.success(f"Log directory {log_dir} is exists.")
    else:
        console.warning(f"Log directory {log_dir} is not exists. We will create it.")
        log_dir.mkdir()
        console.success(f"Log directory {log_dir} was created.")

    # 暂时就这些目录吧。

    # 然后这里做其他的初始化任务, 包括:
    # 1. 生成默认的配置文件
    # 2. 检查qdrant 数据库的连接,初始化数据库
    # 3. 检查 Redis 的连接,初始化 Redis 数据库
    # 4. 检查默认大模型的连接,确保默认大模型可以正常工作

    # 检查默认的配置文件,如果配置文件不存在,则使用默认配置
    with console.status(
        "[cyan]Initial the configuration file ......",
    ):
        time.sleep(2)

    config_file = cli_context.get_default_config_file()

    if config_file.exists():
        # 如果配置文件已经存在,则检查配置想是否正确
        console.success(f"The default configuration file {config_file} is exists.")
    else:
        # 如果配置文件不存在,则生成默认的配置文件:
        console.warning(f"The default configuration file {config_file} is not exists. We will generate it.")
        cli_context.create_default_config_file()
        console.success(f"The default configuration file {config_file} was created.")


    # 生成一个默认的数据库
    with console.status(
        "[cyan]Initial the database ......",
    ):
        time.sleep(2)
    db_file = cli_context.get_default_database()

    console.print(db_file)

    if db_file.exists():
        # 如果数据库文件已经存在了,跳过
        console.success(f"The Database file {db_file} is exists.")
    else:
        # 如果默认的数据库不存在
        console.warning(f"The Database file {db_file} is not exists. We will generate it.")
        console.success(f"The Database file {db_file} was generated.")


