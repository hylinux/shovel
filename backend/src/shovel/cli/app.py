from __future__ import annotations

import typer

from shovel.cli.commands.version import app as version_app

app = typer.Typer(
    name="shovel",
    help="Shovel - Personal AI Agent Platform.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)


# Version 子命令
app.add_typer(
    version_app,
    name="version",
    help="Show Shovel version and runtime information.",
)
