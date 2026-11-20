from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.root_environment import persist_root_environment, show_root_environment
from shovel.cli.ui.console import console
from shovel.config.embedding_config import EmbeddingSettings
from shovel.config.errors import ConfigError
from shovel.config.model_settings import (
    DefaultAgentModelSettings,
    ModelApiMode,
    ModelProviderType,
    foundry_v1_endpoint,
    validate_responses_version,
)
from shovel.config.paths import (
    default_root_path,
    resolve_root_path,
    root_path_scope,
    validate_initialization_root,
)
from shovel.config.settings import AppSettings, load_settings
from shovel.db.database import init_database
from shovel.services.health import HealthResult, HealthStatus, check_model
from shovel.vector import init_vector_store

app = typer.Typer(
    help="Configure a new Shovel profile and initialize local stores.",
    invoke_without_command=True,
    no_args_is_help=False,
)

#: 每个检查步骤前的停顿, 纯粹是为了让 spinner 能被看见。
_STEP_PAUSE_SECONDS = 2

_MODEL_PROVIDERS = (
    ("Azure OpenAI", "azure_openai"),
    ("OpenAI", "openai"),
    ("DeepSeek", "deepseek"),
    ("Qianwen", "qianwen"),
    ("Ollama", "ollama"),
    ("Microsoft Foundry", "azure_foundry"),
)
_EMBEDDING_PROVIDERS = (
    ("Azure OpenAI", "azure_openai"),
    ("OpenAI", "openai"),
    ("Qianwen", "qianwen"),
    ("Ollama", "ollama"),
)
_MODEL_APIS = (
    ("Chat Completions (existing compatibility)", "chat_completions"),
    ("Responses (recommended for Foundry reasoning models and tools)", "responses"),
)


def _prompt_provider(label: str, providers: tuple[tuple[str, str], ...], *, default: int = 1) -> str:
    for number, (name, _) in enumerate(providers, start=1):
        console.print(f"  {number}. {name}")
    while True:
        choice: int = typer.prompt(label, type=int, default=default)
        if 1 <= choice <= len(providers):
            return providers[choice - 1][1]
        console.warning(f"Choose a number from 1 to {len(providers)}.")


def _prompt_required(label: str) -> str:
    while True:
        value: str = typer.prompt(label)
        if value.strip():
            return value.strip()
        console.warning("This value cannot be empty.")


def _prompt_endpoint(label: str, provider: str) -> str:
    while True:
        value = _prompt_required(label)
        try:
            url = urlsplit(value)
            valid = (
                url.scheme in {"http", "https"}
                and bool(url.hostname)
                and (url.port is None or 1 <= url.port <= 65535)
                and not (url.username or url.password or url.query or url.fragment)
                and not any(character.isspace() for character in value)
                and "\\" not in value
            )
        except ValueError:
            valid = False
        if not valid:
            console.warning("Enter an HTTP(S) endpoint without credentials, query or fragment.")
        elif provider == "azure_openai" and url.path.rstrip("/"):
            console.warning("Azure OpenAI requires the resource root endpoint, without a deployment path.")
        elif provider == "azure_foundry":
            try:
                foundry_v1_endpoint(value)
                return value
            except ConfigError as exc:
                console.warning(exc.message)
        else:
            return value


def _prompt_api_version(
    label: str, provider: str, *, api_mode: ModelApiMode = ModelApiMode.CHAT_COMPLETIONS
) -> str:
    while True:
        value: str = typer.prompt(label, default="", show_default=False)
        value = value.strip()
        if not value and provider == "azure_openai":
            console.warning("Azure OpenAI requires an API version.")
            continue
        if api_mode is ModelApiMode.RESPONSES and (
            provider == "azure_openai" or (provider == "azure_foundry" and value)
        ):
            try:
                validate_responses_version(value)
            except ConfigError as exc:
                console.warning(exc.message)
                continue
        return value


def _prompt_store_name(label: str, default: str) -> str:
    while True:
        value: str = typer.prompt(label, default=default)
        if re.fullmatch(r"[A-Za-z0-9_]+", value) and not (os.name == "nt" and os.path.isreserved(value)):
            return value
        console.warning("Use only ASCII letters, digits and underscores; reserved filenames are not allowed.")


def _initial_settings(*, interactive: bool) -> AppSettings:
    model = DefaultAgentModelSettings(max_input_chars=240000, max_output_tokens=32768, timeout_seconds=300)
    embedding = EmbeddingSettings(dimensions=1024, batch_size=8, timeout_seconds=300)
    dimension = 1024
    knowledge_name, memory_name = "shovel_chunk", "shovel_memory"

    if interactive:
        console.info(
            "Configure the chat model. API keys are hidden; an empty Azure OpenAI/Foundry key uses Azure CLI."
        )
        provider = _prompt_provider("Model provider", _MODEL_PROVIDERS)
        endpoint = _prompt_endpoint("Model endpoint", provider)
        name = _prompt_required("Model name / deployment")
        api_mode = ModelApiMode.CHAT_COMPLETIONS
        if provider in ("azure_openai", "azure_foundry", "openai"):
            api_mode = ModelApiMode(
                _prompt_provider("Model API", _MODEL_APIS, default=2 if provider == "azure_foundry" else 1)
            )
        version = _prompt_api_version(
            "Model API version (empty for Foundry v1/non-Azure providers)", provider, api_mode=api_mode
        )
        key: str = typer.prompt(
            "Model API key (model.api_key; may be empty)",
            default="",
            hide_input=True,
            show_default=False,
        )
        model = DefaultAgentModelSettings.model_validate(
            {
                **model.model_dump(),
                "model_provider_type": ModelProviderType(provider),
                "base_endpoint": endpoint,
                "model_name": name,
                "api_version": version,
                "api_mode": api_mode,
                "api_key": key,
            }
        )
        knowledge_name = _prompt_store_name("Knowledge name", knowledge_name)

        console.info("Configure embeddings. Memory will use a copy of these model and embedding settings.")
        provider = _prompt_provider("Embedding provider", _EMBEDDING_PROVIDERS)
        endpoint = _prompt_endpoint("Embedding endpoint", provider)
        name = _prompt_required("Embedding model / deployment")
        while True:
            dimension = typer.prompt("Embedding / index dimensions", type=int, default=1024)
            if dimension > 0:
                break
            console.warning("Dimensions must be a positive integer.")
        if provider == "ollama":
            console.info(
                "Ollama does not accept a dimensions parameter. Its model output must match the index "
                "dimensions; the request parameter will be omitted."
            )
        key = typer.prompt(
            "Embedding API key (may be empty)", default="", hide_input=True, show_default=False
        )
        version = _prompt_api_version("Embedding API version (empty for non-Azure providers)", provider)
        embedding = EmbeddingSettings(
            provider=provider,
            base_url=endpoint,
            model=name,
            dimensions=None if provider == "ollama" else dimension,
            api_key=key,
            api_version=version,
            batch_size=8,
            timeout_seconds=300,
        )
        memory_name = _prompt_store_name("Memory collection name", memory_name)

    return AppSettings.from_file_data(
        {
            "model": model.model_dump(mode="python"),
            "database": {
                "url": f"sqlite+aiosqlite:///{cli_context.get_default_database().as_posix()}",
            },
            "temp_path": cli_context.get_default_temp_dir(),
            "agent": {
                "workspace_path": cli_context.get_default_workspace(),
                "max_tool_iterations": 5,
            },
            "performance": {"max_local_model_requests": 3},
            "zvec": {
                "knowledge_path": str(cli_context.get_default_knowledge_dir() / knowledge_name),
                "chunk_collection": knowledge_name,
                "dim": dimension,
                "log_dir": str(cli_context.get_default_logs_dir()),
            },
            "embedding": embedding.model_dump(mode="python"),
            "memory": {
                "collection": memory_name,
                "index_path": cli_context.get_default_memory_dir() / memory_name,
                "dim": dimension,
                "max_items": 20,
                "history_messages": 10,
                "llm": model.model_dump(mode="python"),
                "embedder": embedding.model_dump(mode="python"),
            },
        }
    )


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

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"Cannot create initialization directory {path} ({type(exc).__name__}).") from None

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
def init(
    no_input: Annotated[
        bool,
        typer.Option("--no-input", help="Create an unconfigured profile without interactive prompts."),
    ] = False,
    root_path: Annotated[
        Path | None, typer.Option("--root-path", help="Initialize here instead of the selected data root.")
    ] = None,
    persist_root: Annotated[
        bool,
        typer.Option("--persist-root", help="After success, save SHOVEL_ROOT_PATH for your Windows user."),
    ] = False,
) -> None:
    selected = resolve_root_path(root_path)
    if not no_input and root_path is None:
        try:
            while True:
                value: str = typer.prompt("Shovel initialization root", default=str(selected))
                try:
                    selected = resolve_root_path(value)
                    validate_initialization_root(selected)
                    break
                except ConfigError as exc:
                    console.warning(exc.message)
        except typer.Abort:
            console.warning("Initialization cancelled; configuration was not saved.")
            raise typer.Exit(code=1) from None
    with root_path_scope(selected):
        validate_initialization_root(selected)
        _initialize(no_input=no_input)

    if selected != default_root_path() or persist_root:
        show_root_environment(selected)
        save_root = persist_root
        if not save_root and not no_input and os.name == "nt":
            try:
                save_root = typer.confirm("Save this root for your Windows user?", default=False)
            except typer.Abort:
                console.warning("Initialization completed; environment persistence cancelled.")
        if save_root:
            persist_root_environment(selected)


def _initialize(*, no_input: bool) -> None:
    console.info("Begin initial Shovel Agent.")

    config_file = cli_context.get_default_config_file()
    initial: AppSettings | None = None
    if not config_file.exists():
        try:
            initial = _initial_settings(interactive=not no_input)
        except typer.Abort:
            console.warning("Initialization cancelled; configuration was not saved.")
            raise typer.Exit(code=1) from None

    # check profile 文件目录
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
        (cli_context.get_default_temp_dir, "temporary attachment directory"),
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

    if config_file.exists():
        # 如果配置文件已经存在,则检查配置想是否正确
        console.success(f"The default configuration file {config_file} is exists.")
    else:
        # 如果配置文件不存在,则生成默认的配置文件:
        console.warning(f"The default configuration file {config_file} is not exists. We will generate it.")
        assert initial is not None
        initial.save(config_file)
        console.success(f"The default configuration file {config_file} was created.")

    # 读取配置: 数据库 URL 与向量库参数都以配置文件为准,
    # 不再各处硬编码 ~/.shovel 下的路径
    settings = load_settings(config_file)
    _ensure_dir(settings.temp_path.expanduser(), "configured temporary directory")

    for name in ("skills", "functions", "pipeline"):
        _ensure_dir(settings.agent.workspace / name, f"Agent {name} directory")

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
            f"SQLite schema is ready, {len(created_tables)} table(s) created: {', '.join(created_tables)}"
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
            console.success(f"Zvec collection '{info.name}' is exists at {info.path}.")

    settings.memory.index_path.expanduser().mkdir(parents=True, exist_ok=True)
    console.success("Native Memory is ready in SQLite; its Zvec index initializes on demand.")

    # 检查默认大模型的连接。未配置时只是提示, 不算失败 —— 见 services/health.py
    with console.status(
        "[cyan]Checking the default model endpoint ......",
    ):
        time.sleep(_STEP_PAUSE_SECONDS)

    _report_health("Default model", check_model(settings.model))

    console.success("Shovel Agent was initialized.")
