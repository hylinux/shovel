from __future__ import annotations

from enum import Enum

from pydantic_settings import BaseSettings


class DocumentType(Enum):
    TEXT = 1
    CSV  = 2
    MARKDOWN = 3
    PDF = 4
    WORD = 5
    HTML = 6
    XML = 7


class DocumentSettings(BaseSettings):
    root_dir: str | None = None
