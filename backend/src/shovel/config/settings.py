from __future__ import annotations

import json
from pathlib import Path

from pydantic_settings import BaseSettings

from .document_settings import DocumentSettings
from .model_settings import DefaultAgentModelSettings
from .qdrant_config import QdrantSettings
from .redis_config import RedisSettings


class AppSettings(BaseSettings):
    service_name: str = "shovel"
    agent_host: str = "127.0.0.1"
    agent_port: int = 19566
    http_host: str = "127.0.0.1"
    http_port: int = 8080
    log_level: str = "INFO"

    document: DocumentSettings = DocumentSettings()
    qdrant: QdrantSettings = QdrantSettings()
    redis:  RedisSettings = RedisSettings()
    model:  DefaultAgentModelSettings = DefaultAgentModelSettings()


    @classmethod
    def load(cls, path: Path) -> AppSettings:
        return cls.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def save(self, path:Path ) -> None:
        path.write_text(
            self.model_dump_json(indent=4),
            encoding="utf-8",
        )


