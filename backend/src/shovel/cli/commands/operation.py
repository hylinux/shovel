from __future__ import annotations

import asyncio
import json
from typing import Annotated

import typer
from sqlalchemy import select

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.config.settings import AppSettings, load_settings
from shovel.db.database import create_async_database_engine, create_async_session_factory
from shovel.db.model.runtime import Operation
from shovel.runtime.api import operation_view
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
    async def read() -> list[dict[str, object]]:
        settings = _settings()
        context = create_async_database_engine(settings.database.url)
        engine = await anext(context)
        try:
            statement = select(Operation).order_by(Operation.queued_at.desc(), Operation.id).limit(limit)
            if state is not None:
                statement = statement.where(Operation.state == state)
            async with create_async_session_factory(engine)() as session:
                return [operation_view(row) for row in await session.scalars(statement)]
        finally:
            await context.aclose()

    console.print_json(data=asyncio.run(read()))


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
