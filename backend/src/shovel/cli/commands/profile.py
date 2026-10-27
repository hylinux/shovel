from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select

from shovel.cli.context import cli_context
from shovel.cli.decorators import command_handler
from shovel.cli.ui.console import console
from shovel.config.settings import load_settings
from shovel.db.database import create_async_database_engine, create_async_session_factory
from shovel.db.model.config import ScanProfile
from shovel.runtime.client import execute_command
from shovel.runtime.profiles import profile_view
from shovel.runtime.schemas import ProfileInput
from shovel.services.resource_service import ResourceService

app = typer.Typer(no_args_is_help=True, help="Manage persisted scan profiles.")


@app.command("list")
@command_handler()
def list_profiles(resource: str) -> None:
    async def read() -> list[dict[str, object]]:
        settings = load_settings(cli_context.get_default_config_file())
        context = create_async_database_engine(settings.database.url)
        engine = await anext(context)
        try:
            sessions = create_async_session_factory(engine)
            row = await ResourceService(sessions).resolve(resource)
            async with sessions() as session:
                profiles = await session.scalars(
                    select(ScanProfile)
                    .where(
                        ScanProfile.resource_id == row.id,
                    )
                    .order_by(ScanProfile.name)
                )
                return [profile_view(profile) for profile in profiles]
        finally:
            await context.aclose()

    console.print_json(data=asyncio.run(read()))


@app.command("save")
@command_handler()
def save(
    resource: str,
    name: str,
    file: Annotated[Path, typer.Option(exists=True, dir_okay=False, readable=True)],
) -> None:
    request = ProfileInput.model_validate_json(file.read_text(encoding="utf-8"))
    console.print_json(
        data=asyncio.run(
            execute_command(
                load_settings(cli_context.get_default_config_file()),
                "profile.save",
                {"name": name, "profile": request.model_dump(exclude_none=True)},
                ref=resource,
                request_key=cli_context.request_key,
            )
        )
    )


@app.command("remove")
@command_handler()
def remove(resource: str, name: str, yes: Annotated[bool, typer.Option("--yes", "-y")] = False) -> None:
    if not yes and not typer.confirm("Delete profile, its scan history and exclusively owned index entries?"):
        raise typer.Abort()
    console.print_json(
        data=asyncio.run(
            execute_command(
                load_settings(cli_context.get_default_config_file()),
                "profile.delete",
                {"name": name, "profile": None},
                ref=resource,
                request_key=cli_context.request_key,
            )
        )
    )
