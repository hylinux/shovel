from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import ParamSpec, TypeVar

import typer

from shovel.cli.exception import CliExceptionHandler
from shovel.config.errors import ConfigError
from shovel.config.paths import root_path_scope

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
                override = kwargs.get("root_path")
                if override is not None and not isinstance(override, (str, Path)):
                    raise ConfigError("Root path must be a directory path.")
                with root_path_scope(override):
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
