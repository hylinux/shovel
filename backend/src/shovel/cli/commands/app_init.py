from __future__ import annotations

from pathlib import Path

import typer

from shovel.cli.context import CliContext
from shovel.cli.ui.console import console

app = typer.Typer(
    help="Initial shovel agent",
    invoke_without_command=True,
    no_args_is_help=False
)


@app.callback()
def init() -> None:
    cli_context = CliContext()

    console.print("Begin initial Shovel Agent.")

    console.print("Checking the default Profile directory.")

    profile_path = cli_context.get_default_profile_dir()

    console.print(f"The default profile direct is: {profile_path}")

    if Path.exists(profile_path):
        console.print(f"The default profile directory {profile_path} is exists.")
    else:
        console.print(f"The default profile directory {profile_path} is not eixts. We will create it.")


