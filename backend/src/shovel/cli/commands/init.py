from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import typer

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.config.settings import load_settings
from shovel.db.database import init_database
from shovel.memory import init_memory_store
from shovel.services.health import HealthResult, HealthStatus, check_model
from shovel.vector import init_vector_store

app = typer.Typer(
    help="Initial shovel agent",
    invoke_without_command=True,
    no_args_is_help=False
)

#: 每个检查步骤前的停顿, 纯粹是为了让 spinner 能被看见。
_STEP_PAUSE_SECONDS = 2


def _ensure_dir(path: Path, label: str) -> None:
    """确保目录存在。

    一律带上 ``parents=True, exist_ok=True``:

    * ``parents``  —— 这些目录之间有父子关系(knowledge / memory / data 都在
      profile 之下)。逐个裸 ``mkdir()`` 依赖"上一步一定成功"这个假设,
      一旦顺序被调整或某一层被用户删掉, 报出来的是一个完全没有上下文的
      ``FileNotFoundError``。
    * ``exist_ok`` —— "存在性检查"和"创建"之间永远有时间差。两个终端同时
      跑 ``shovel init`` 时, 后一个会撞上 ``FileExistsError`` 而整个失败,
      而目录已经存在恰恰是我们想要的结果。
    """

    with console.status(f"[cyan]Checking the {label} ......"):
        time.sleep(_STEP_PAUSE_SECONDS)

    existed = path.exists()

    if not existed:
        console.warning(f"The {label} {path} is not exists. We will create it.")

    path.mkdir(parents=True, exist_ok=True)

    if existed:
        console.success(f"The {label} {path} is exists.")
    else:
        console.success(f"The {label} {path} was created.")


def _report_health(label: str, result: HealthResult) -> None:
    """把自检结果翻译成终端输出。

    失败只 warning 不抛异常: init 的职责是把本地环境准备好, 而外部依赖
    连不上通常意味着"还没配", 不该让整个初始化功亏一篑。
    """

    if result.status is HealthStatus.OK:
        console.success(f"{label}: {result.detail}")
    elif result.status is HealthStatus.SKIPPED:
        console.info(f"{label}: {result.detail}")
    else:
        console.warning(f"{label}: {result.detail}")


@app.callback()
@command_handler()
def init() -> None:

    console.info("Begin initial Shovel Agent.")

    #check profile 文件目录
    console.info("Checking the default directories.")

    # 目录之间有父子关系, 所以 profile 必须排在最前面
    directories: list[tuple[Callable[[], Path], str]] = [
        (cli_context.get_default_profile_dir, "default profile directory"),
        (cli_context.get_default_config_dir, "default configuration directory"),
        (cli_context.get_default_workspace, "default workspace directory"),
        # 这里暂时采用 sqlite 来管理运行部分需要数据库的地方
        (cli_context.get_default_database_dir, "database directory"),
        # 知识与记忆各自独立落盘: 删掉知识库重建时不会碰到记忆
        (cli_context.get_default_knowledge_dir, "knowledge directory"),
        (cli_context.get_default_memory_dir, "memory directory"),
        (cli_context.get_default_logs_dir, "log directory"),
    ]

    for resolve, label in directories:
        _ensure_dir(resolve(), label)

    # 暂时就这些目录吧。

    # 然后这里做其他的初始化任务, 包括:
    # 1. 生成默认的配置文件
    # 2. 检查向量库/记忆库, 初始化它们
    # 3. 检查默认大模型的连接,确保默认大模型可以正常工作

    # 检查默认的配置文件,如果配置文件不存在,则使用默认配置
    with console.status(
        "[cyan]Initial the configuration file ......",
    ):
        time.sleep(_STEP_PAUSE_SECONDS)

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
        time.sleep(_STEP_PAUSE_SECONDS)

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
        time.sleep(_STEP_PAUSE_SECONDS)

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
        time.sleep(_STEP_PAUSE_SECONDS)

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

    # 检查默认大模型的连接。未配置时只是提示, 不算失败 —— 见 services/health.py
    with console.status(
        "[cyan]Checking the default model endpoint ......",
    ):
        time.sleep(_STEP_PAUSE_SECONDS)

    _report_health("Default model", check_model(settings.model))

    console.success("Shovel Agent was initialized.")
