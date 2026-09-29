from __future__ import annotations

import time
from pathlib import Path

import typer

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.config.settings import load_settings
from shovel.db.database import init_database
from shovel.memory import init_memory_store
from shovel.vector import init_vector_store

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
        console.warning(f"Database directory {db_dir} is not exists. We will create it.")
        db_dir.mkdir()
        console.success(f"Database directory {db_dir} was created.")


    # 需要新增两个目录
    # 一个用于存放knowledge, 一个用于存放记忆
    with console.status(
        "[cyan]Checking the Knowledge directory ......",
    ):
        time.sleep(2)

    knowledge_dir = cli_context.get_default_knowledge_dir()

    if Path.exists(knowledge_dir):
        console.success(f"Knowledge directory {knowledge_dir} is exists.")
    else:
        console.warning(f"Knowledge directory {knowledge_dir} is not exists. We will create it.")
        knowledge_dir.mkdir()
        console.success(f"Knowledge directory {knowledge_dir} was created.")


    # 用于存放记忆的目录
    with console.status(
        "[cyan]Checking the Memory directory ......",
    ):
        time.sleep(2)

    memory_dir = cli_context.get_default_memory_dir()

    if Path.exists(memory_dir):
        console.success(f"Memory directory {memory_dir} is exists.")
    else:
        console.warning(f"Memory directory {memory_dir} is not exists. We will create it.")
        memory_dir.mkdir()
        console.success(f"Memory directory {memory_dir} was created.")



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


    # 读取配置: 数据库 URL 与向量库参数都以配置文件为准,
    # 不再各处硬编码 ~/.shovel 下的路径
    settings = load_settings(config_file)

    # 初始化 SQLite: 建表是幂等的, 已存在的表不会被动
    with console.status(
        "[cyan]Initial the database ......",
    ):
        time.sleep(2)

    db_file = cli_context.get_default_database()

    if db_file.exists():
        console.success(f"The Database file {db_file} is exists.")
    else:
        console.warning(f"The Database file {db_file} is not exists. We will generate it.")

    created_tables = init_database(
        settings.database.url,
        echo=settings.database.echo,
    )

    if created_tables:
        console.success(
            f"SQLite schema is ready, {len(created_tables)} table(s) created: "
            f"{', '.join(created_tables)}"
        )
    else:
        console.success("SQLite schema is already up to date. Nothing to create.")

    # 初始化 Zvec: 只负责知识库(chunk) collection。
    # 已存在时绝不重建 —— 重建等于丢掉全部向量, 而重新 embedding 是最贵的一步。
    with console.status(
        "[cyan]Initial the knowledge vector store (Zvec) ......",
    ):
        time.sleep(2)

    collections = init_vector_store(settings.zvec)

    for info in collections:
        if info.created:
            console.success(
                f"Zvec collection '{info.name}' was created at {info.path} "
                f"(dim={settings.zvec.dim}, metric={settings.zvec.metric})."
            )
        else:
            console.success(
                f"Zvec collection '{info.name}' is exists at {info.path}."
            )

    # 初始化记忆: mem0 + 本地 Qdrant。
    # 记忆不走 Zvec, 因为记忆的写入/冲突/失效逻辑交给了 mem0, 而 mem0
    # 不支持 Zvec —— 详见 shovel/config/memory_config.py。
    #
    # 这一步只建 collection, 不实例化 LLM 与 embedder:
    # init 必须能在用户还没填 API Key 的时候跑完。
    with console.status(
        "[cyan]Initial the memory store (mem0 + Qdrant) ......",
    ):
        time.sleep(2)

    memory_info = init_memory_store(settings.memory)

    if memory_info.created:
        console.success(
            f"Qdrant collection '{memory_info.collection}' was created at "
            f"{memory_info.location} (mode={memory_info.mode}, dim={memory_info.dim})."
        )
    else:
        console.success(
            f"Qdrant collection '{memory_info.collection}' is exists at "
            f"{memory_info.location}."
        )

    console.success(f"mem0 history database is ready at {memory_info.history_db}.")

    console.success("Shovel Agent was initialized.")

