from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import typer

from shovel.cli.exception import CliExceptionHandler

P = ParamSpec("P")
R = TypeVar("R")

def command_handler() -> Callable[
    [Callable[P, R]],
    Callable[P, R | None],
]:

    def decorator(
            func: Callable[P, R],
    ) -> Callable[P, R | None]:

        @wraps(func)
        def wrapper(
            *args: P.args,
            **kwargs: P.kwargs,
        ) -> R | None:

            try:

                return func(
                    *args,
                    **kwargs,
                )
            except typer.Exit:
                raise

            except Exception as exc:
                handler = CliExceptionHandler()
                handler.handle(exc)

        return wrapper

    return decorator

