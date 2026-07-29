from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True, kw_only=True)
class Symbols:
    SUCCESS = "✓"
    ERROR = "✗"
    WARNING = "⚠"
    INFO = "ℹ"  # noqa: RUF001
    DEBUG = "•"
