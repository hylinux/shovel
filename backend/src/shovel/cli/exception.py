from __future__ import annotations

import os
from typing import NoReturn

import typer

from shovel.cli.ui.console import console
from shovel.exceptions.base import ShovelError
from shovel.exceptions.exit_codes import ExitCodes


def is_debug_enabled() -> bool:

    value = os.getenv(
        "SHOVEL_DEBUG",
        "",
    )

    return value.lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class CliExceptionHandler:
    """
    针对cli 应用的异常处理
    """

    def __init__(
            self,
            *,
            debug: bool | None = None,
    ) -> None:
        self._debug = (
            is_debug_enabled() 
            if debug is None
            else debug
        )


    def handle(
            self,
            exc: Exception,
    ) -> NoReturn:

        if isinstance(
            exc,
            ShovelError,
        ):
            self._handle_shovel_error(exc)

        self._handle_unexpected_error(exc)


    def _handle_shovel_error(
            self,
            exc: ShovelError,
    ) -> NoReturn:
        console.error(exc.message)

        if exc.hint:
            console.info(f"Hint: {exc.hint}")

        if self._debug:
            console.debug(f"Exit Code: {exc.exit_code}")

            if exc.details:
                console.debug(f"Details: {exc.details}")


        raise typer.Exit(exc.exit_code)


    def _handle_unexpected_error(
            self,
            exc: Exception,
    ) -> NoReturn:

        console.error("An unexpected error occurred.")

        if self._debug:
            console.warning("Debug mode is enabled. Full trackback follows:")
            console.print_exception()

        else:
            console.info(
                "Run with '--debug' or set SHOVEL_DEBUG=1 to see the full traceback."
            )


        raise typer.Exit(ExitCodes.GENERAL_ERROR)

