from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.config.settings import load_settings
from shovel.runtime.client import execute_command
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.schemas import IndexOptimizeInput

app = typer.Typer(no_args_is_help=True, help="Explicit knowledge-index maintenance; never automatic.")


@app.command("optimize")
@command_handler()
def optimize(
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Acknowledge the maintenance window and temporary vector unavailability."),
    ] = False,
    concurrency: Annotated[int, typer.Option(min=1, max=2, help="Native maintenance threads (1-2).")] = 2,
) -> None:
    if not yes:
        raise RuntimeFailure("Pass --yes to acknowledge the knowledge-index maintenance window.")
    settings = load_settings(cli_context.get_default_config_file())
    request = IndexOptimizeInput(maintenance_window=True, concurrency=concurrency)
    receipt = asyncio.run(
        execute_command(settings, "index.optimize", request.model_dump(), request_key=cli_context.request_key)
    )
    console.print_json(data=receipt)
