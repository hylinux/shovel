from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from shovel.config.errors import ConfigError

ROOT_ENVIRONMENT = "SHOVEL_ROOT_PATH"
_ROOT: ContextVar[Path | None] = ContextVar("shovel_root_path", default=None)


def default_root_path() -> Path:
    return (Path.home() / ".shovel").resolve()


def resolve_root_path(value: str | Path | None = None) -> Path:
    if value is None:
        if selected := _ROOT.get():
            return selected
        value = os.environ.get(ROOT_ENVIRONMENT, "").strip()
        if not value:
            value = default_root_path()
        elif not Path(value).expanduser().is_absolute():
            raise ConfigError("SHOVEL_ROOT_PATH must be an absolute directory path.")
    try:
        text = str(value).strip()
        if not text or "\0" in text or (os.name == "nt" and os.path.isreserved(text)):
            raise ValueError("Invalid directory path")
        path = Path(text).expanduser().resolve()
        parent = path
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if not parent.is_dir():
            raise ConfigError(f"Shovel root has a file instead of a directory: {parent}")
        return path
    except (OSError, ValueError, RuntimeError) as exc:
        raise ConfigError(f"Cannot resolve Shovel root directory ({type(exc).__name__}).") from None


@contextmanager
def root_path_scope(value: str | Path | None = None) -> Iterator[Path]:
    root = resolve_root_path(value)
    token = _ROOT.set(root)
    try:
        yield root
    finally:
        _ROOT.reset(token)


def validate_initialization_root(root: Path) -> None:
    parent = root
    while not parent.exists():
        parent = parent.parent
    if not os.access(parent, os.W_OK | os.X_OK):
        raise ConfigError(f"Shovel initialization directory is not writable: {parent}")
