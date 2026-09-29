#---------------------------------------------------------------------
# 'shovel config' —— 配置文件的生成/校验/查看
#
# 存在的理由: config/settings.py 里的多条报错都写着 "执行 'shovel config'
# 生成默认配置" / "使用 'shovel config --force'", 而这个命令此前并不存在。
# 报错文案指向一个不存在的命令, 比不给建议更糟。
#
# 日期: 2026-09-29
# 作者: HongWei Guo <hongweig@163.com>
#---------------------------------------------------------------------
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.config.settings import load_settings

app = typer.Typer(
    help="Show, validate or regenerate the Shovel configuration file.",
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback()
@command_handler()
def config(
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Regenerate the configuration file from defaults. "
                "The existing file is backed up first."
            ),
        ),
    ] = False,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help=(
                "Validate the existing configuration file without "
                "creating or modifying anything."
            ),
        ),
    ] = False,
    show: Annotated[
        bool,
        typer.Option(
            "--show",
            help="Print the current configuration file.",
        ),
    ] = False,
) -> None:
    """Manage ``~/.shovel/config/settings.toml``."""

    config_file = cli_context.get_default_config_file()

    if show:
        _show(config_file)
        return

    if force:
        _regenerate(config_file)
        return

    if check:
        # --check 绝不创建文件: 它存在的意义就是"只看, 不动"。
        # 校验命令顺手生成一份配置, 会让"配置丢了"这件事被掩盖过去。
        load_settings(config_file)
        console.success(f"Configuration file {config_file} is valid.")
        return

    if not config_file.exists():
        cli_context.create_default_config_file()
        console.success(f"Default configuration file created at {config_file}.")
        return

    # 文件已经存在: 不动它, 只把它读一遍, 好让错别字/类型错误当场暴露
    load_settings(config_file)
    console.success(f"Configuration file {config_file} is valid.")


def _show(config_file: Path) -> None:
    if not config_file.exists():
        console.warning(
            f"Configuration file {config_file} does not exist. "
            f"Run 'shovel config' to generate it."
        )
        return

    # markup=False: TOML 的 [table] 头与 rich 的样式标签撞车,
    # 默认渲染会把每一个段名都吃掉, 打印出来的配置将无法使用。
    console.print(
        config_file.read_text(encoding="utf-8"),
        markup=False,
        highlight=False,
    )


def _regenerate(config_file: Path) -> None:
    existed = config_file.exists()

    backup = cli_context.create_default_config_file(force=True)

    if backup is not None:
        console.warning(f"The previous configuration was backed up to {backup}.")

    verb = "regenerated" if existed else "created"
    console.success(f"Configuration file {config_file} was {verb}.")
