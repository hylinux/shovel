from __future__ import annotations

from pydantic import BaseModel


class RedisSettings(BaseModel):
    host: str | None = None
    port: int | None = None

