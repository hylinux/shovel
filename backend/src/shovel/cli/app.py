from __future__ import annotations

import os

import typer

from shovel.cli.commands.version import app as version_app

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
) -> None:

    if debug:
        os.environ["SHOVEL_DEBUG"] = "1"


# Version 子命令
app.add_typer(
    version_app,
    name="version",
    help="Show Shovel version and runtime information.",
)
