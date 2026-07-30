from __future__ import annotations

from pydantic_settings import BaseSettings


class QdrantSettings(BaseSettings):
    host: str | None = None
    port: int | None = None
