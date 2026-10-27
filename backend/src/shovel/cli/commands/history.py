from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.config.settings import load_settings
from shovel.runtime.client import execute_command
from shovel.runtime.schemas import HistoryPruneInput

app = typer.Typer(no_args_is_help=True, help="Explicit, bounded maintenance of terminal task history.")


@app.command("prune")
@command_handler()
def prune(
    keep_days: Annotated[int, typer.Option(min=1, max=36500)] = 30,
    limit: Annotated[int, typer.Option(min=1, max=10000)] = 1000,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    if not yes and not typer.confirm(
        f"Delete up to {limit} old records per history category, keeping the latest {keep_days} days?"
    ):
        raise typer.Abort()
    request = HistoryPruneInput(keep_days=keep_days, limit=limit)
    console.print_json(
        data=asyncio.run(
            execute_command(
                load_settings(cli_context.get_default_config_file()),
                "history.prune",
                request.model_dump(),
                request_key=cli_context.request_key,
            )
        )
    )
