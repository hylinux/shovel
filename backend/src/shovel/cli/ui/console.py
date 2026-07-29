from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.text import Text

from shovel.cli.ui.styles import SHOVEL_CLI_THEME
from shovel.cli.ui.symbols import Symbols


class ConsoleService:

    def __init__(self) -> None:

        self._console = Console(
            theme=SHOVEL_CLI_THEME,
            soft_wrap=True,
        )


    def print(
            self,
            *objects: Any,
            **kwargs: Any,
    ) -> None:

        self._console.print(
            *objects,
            **kwargs,
        )


    def print_json(
            self,
            *objects: Any,
            **kwargs: Any,
    ) -> None:
        self._console.print_json(
            *objects,
            **kwargs,
        )



    def success(
            self,
            message: str,
    ) -> None:
        self._console.print(
            f"{Symbols.SUCCESS} [success]{message}[/success]",
        )


    def error(
            self,
            message: str,
    ) -> None:
        self._console.print(
            f"{Symbols.ERROR} [error]{message}[/error]"
        )


    def warning(
            self,
            message: str,
    ) -> None:
        self._console.print(
            f"{Symbols.WARNING} [warning]{message}[/warning]"
        )


    def info(
            self,
            message: str,
    ) -> None:
        self._console.print(
            f"{Symbols.INFO} [info]{message}[/info]"
        )


    def debug(
            self,
            message: str,
    ) -> None:
        self._console.print(
            f"{Symbols.DEBUG} [debug]{message}[/debug]"
        )


    def error_panel(
            self,
            message: str,
            *,
            title: str = "Error",
    ) -> None:
        self._console.print(
            Panel(
                Text(message),
                title=title,
                border_style="error",
                width=80,
                padding=(1, 2),
            )
        )


    def warning_panel(
            self,
            message: str,
            *,
            title: str = "Warning",
    ) -> None:
        self._console.print(
            Panel(
                Text(message),
                title=title,
                border_style="warning",
                width=80,
                padding=(1, 2),
            )

        )

    def info_panel(
            self,
            message: str,
            *,
            title: str = "Info",
    ) -> None:

        self._console.print(
            Panel(
                Text(message),
                title=title,
                border_style="info",
                padding=(1, 2),
                width=80,
            )
        )

    def title_panel(
            self,
            title: str,
            message: str,
            width: int = 80,
    ) -> None:
        self._console.print(
            Panel(
                Text(message),
                title=title,
                border_style="info",
                padding=(1, 2),
                width=width,
            )
        )

    def about_panel(
            self,
            message: str,
            width: int = 80,
    ) -> None:

        self._console.print(
            Panel.fit(
                message,
                title="[bold green]About[/]",
                width=width,
                padding=(1, 2),
                border_style="green",
            )
        )

    def print_panel(
            self,
            objects: RenderableType,
            border_style: str = "bright_blue",
    ) -> None:
        self._console.print(
            Panel.fit(
                objects,
                border_style=border_style,
                padding=(1, 2)

            )
        )


    def print_exception(self) -> None:
        self._console.print_exception(
            show_locals=False,
        )


    @contextmanager
    def status(
        self,
        message: str,
    ) -> Generator[None]:
        with self._console.status(message):
            yield


console = ConsoleService()

