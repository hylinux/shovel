from __future__ import annotations

from rich.box import SIMPLE
from rich.table import Table


def _create_base_table(title: str) -> Table:
    return Table(
        title=title,
        show_header=False,
        box=SIMPLE,
        border_style="bright_blue",
        padding=(0, 3),
        expand=False,
    )


def create_basic_product_table(
    *,
    name: str,
    version: str,
    author: str,
) -> Table:
    table = _create_base_table(
        "Product Information",
    )

    table.add_column(
        style="bold bright_cyan",
        no_wrap=True,
        min_width=16,
    )

    table.add_column(
        style="white",
        min_width=28,
    )

    table.add_row(
        "📦 Name",
        name,
    )

    table.add_row(
        "🏷 Version",
        version,
    )

    table.add_row(
        "👨 Author",
        author,
    )

    return table


def create_environment_table(
    *,
    shovel_version: str,
    python_version: str,
    os_name: str,
    generic_host_version: str,
    frontend_version: str,
    backend_version: str,
) -> Table:
    table = _create_base_table(
        "Environment Information",
    )

    table.add_column(
        style="bold bright_cyan",
        min_width=24,
    )

    table.add_column(
        style="white",
        min_width=28,
    )

    table.add_row(
        "📦 Shovel Version",
        shovel_version,
    )

    table.add_row(
        "🐍 Python Version",
        python_version,
    )

    table.add_row(
        "💻 Platform",
        os_name,
    )

    table.add_row(
        "⚙ Generic Host",
        generic_host_version,
    )

    table.add_row(
        "🌐 Frontend Version",
        frontend_version,
    )

    table.add_row(
        "🚀 Backend Version",
        backend_version,
    )

    return table


def create_workspace_table(
    *,
    workspace: str,
    config_file: str,
    database_file: str,
) -> Table:
    table = _create_base_table(
        "Workspace",
    )

    table.add_column(
        style="bold bright_cyan",
        min_width=18,
    )

    table.add_column(
        style="white",
        min_width=40,
    )

    table.add_row(
        "📂 Workspace",
        workspace,
    )

    table.add_row(
        "📝 Config File",
        config_file,
    )

    table.add_row(
        "🗄 Database",
        database_file,
    )

    return table


def create_runtime_table(
    *,
    configuration_status: str,
    database_status: str,
    model_provider: str,
) -> Table:
    table = _create_base_table(
        "Runtime",
    )

    table.add_column(
        style="bold bright_cyan",
        min_width=18,
    )

    table.add_column(
        style="white",
        min_width=28,
    )

    table.add_row(
        "⚙ Configuration",
        configuration_status,
    )

    table.add_row(
        "🗄 Database",
        database_status,
    )

    table.add_row(
        "🤖 Model Provider",
        model_provider,
    )

    return table
