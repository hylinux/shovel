from __future__ import annotations

import json
import platform
from dataclasses import asdict, dataclass
from typing import Annotated

from rich.panel import Panel
import typer

from shovel.cli.context import cli_context
from shovel.cli.metadata import (
    PRODUCT,
)
from shovel.cli.ui.console import console
from shovel.cli.ui.logo import get_logo, get_tagline
from shovel.cli.ui.tables import (
    create_basic_product_table,
    create_environment_table,
    create_runtime_table,
    create_workspace_table,
)

app = typer.Typer(
    help="Show Shovel version and runtime information.",
    invoke_without_command=True,
    no_args_is_help=False,
)


@dataclass(slots=True, frozen=True, kw_only=True)
class VersionInfo:
    shovel_version: str
    python_version: str
    os_name: str

    workspace: str
    config_file: str
    database_file: str

    configuration_loaded: bool
    database_ready: bool

    model_provider: str

    generic_host_version: str
    frontend_version: str
    backend_version: str


@app.callback()
def version(
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Show detailed runtime information.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Output version information as JSON.",
        ),
    ] = False,
) -> None:
    """
    Show Shovel version information.
    """

    info = collect_version_info()

    if json_output:
        print_json(info)
        return

    if verbose:
        print_verbose_version(info)
        return

    print_basic_version(info)


def collect_version_info() -> VersionInfo:

    config_file = cli_context.get_default_config_file()
    database_file = cli_context.get_default_database()

    return VersionInfo(
        shovel_version=cli_context.get_shovel_version(),
        python_version=platform.python_version(),
        os_name= cli_context.get_os_name(),
        workspace= str(cli_context.get_default_workspace()),
        config_file=str(config_file),
        database_file=str(database_file),
        configuration_loaded=config_file.exists(),
        database_ready=database_file.exists(),
        model_provider="OpenAI",
        generic_host_version=cli_context.get_generic_host_version(),
        frontend_version="0.1.0",
        backend_version=cli_context.get_shovel_version(),
    )


def print_basic_version(info: VersionInfo) -> None:
    console.print()

    console.print(get_logo())
    console.print(get_tagline())

    console.print()

    console.about_panel(
        message=PRODUCT.description
    )


    console.print()

    table = create_basic_product_table(
        name=PRODUCT.name,
        version=info.shovel_version,
        author=PRODUCT.author,
    )

    console.print_panel(table)
    console.print()


def print_verbose_version(info: VersionInfo) -> None:
    console.print()

    console.print(get_logo())
    console.print(get_tagline())

    console.print()

    console.about_panel(PRODUCT.description)


    console.print()

    console.print(
        create_environment_table(
            shovel_version=info.shovel_version,
            python_version=info.python_version,
            os_name=info.os_name,
            generic_host_version=info.generic_host_version,
            frontend_version=info.frontend_version,
            backend_version=info.backend_version,
        )
    )

    console.print()

    console.print(
        create_workspace_table(
            workspace=info.workspace,
            config_file=info.config_file,
            database_file=info.database_file,
        )
    )

    console.print()

    console.print(
        create_runtime_table(
            configuration_status=format_configuration_status(
                info.configuration_loaded,
            ),
            database_status=format_database_status(
                info.database_ready,
            ),
            model_provider=info.model_provider,
        )
    )

    console.print()


def print_json(info: VersionInfo) -> None:
    console.print_json(
        json.dumps(
            asdict(info),
            ensure_ascii=False,
            indent=2,
        )
    )



def format_configuration_status(loaded: bool) -> str:
    if loaded:
        return "[green]✓ Loaded[/]"

    return "[yellow]⚠ Not Initialized[/]"


def format_database_status(ready: bool) -> str:
    if ready:
        return "[green]✓ SQLite Ready[/]"

    return "[yellow]⚠ SQLite Not Initialized[/]"
