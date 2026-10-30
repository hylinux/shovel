from __future__ import annotations

import asyncio
import json
from typing import Annotated

import typer

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.config.settings import AppSettings, load_settings
from shovel.runtime.client import list_operations as query_operations
from shovel.runtime.client import manage_operation
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.locking import endpoint_file

app = typer.Typer(no_args_is_help=True, help="Find, inspect and resume durable operations.")


def _settings() -> AppSettings:
    return load_settings(cli_context.get_default_config_file())


@app.command("list")
@command_handler()
def list_operations(
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 20,
    state: Annotated[str | None, typer.Option()] = None,
) -> None:
    console.print_json(data=asyncio.run(query_operations(_settings(), limit=limit, state=state)))


@app.command("requests")
@command_handler()
def requests() -> None:
    """List local pre-submission receipts, including requests whose response was lost."""
    directory = endpoint_file(_settings()).parent / "requests"
    records: list[object] = []
    for path in sorted(directory.glob("op_request_*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise RuntimeFailure(f"无法读取请求回执: {path}") from exc
    console.print_json(data=records)


@app.command("show")
@command_handler()
def show(identifier: str) -> None:
    console.print_json(data=asyncio.run(manage_operation(_settings(), identifier)))


@app.command("wait")
@command_handler()
def wait(identifier: str) -> None:
    """Reconnect to a submitted operation, starting a non-Web Host only when needed."""
    receipt = asyncio.run(manage_operation(_settings(), identifier, wait=True))
    console.print_json(data=receipt)
    if receipt["state"] != "succeeded":
        raise typer.Exit(1)


@app.command("retry")
@command_handler()
def retry(identifier: str) -> None:
    """Retry the existing operation and wait without creating another receipt."""
    receipt = asyncio.run(manage_operation(_settings(), identifier, retry=True, wait=True))
    console.print_json(data=receipt)
    if receipt["state"] != "succeeded":
        raise typer.Exit(1)
