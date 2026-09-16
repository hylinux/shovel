from __future__ import annotations

from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
)

from .database_config import DatabaseSettings
from .document_settings import DocumentSettings
from .model_settings import DefaultAgentModelSettings
from .qdrant_config import QdrantSettings
from .redis_config import RedisSettings


def load_settings(config_path: Path | str) -> AppSettings:
    """从指定 JSON 文件加载 Shovel 配置。"""

    path = Path(config_path).expanduser().resolve()

    class JsonSettings(AppSettings):

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return (
                init_settings,
                env_settings,
                JsonConfigSettingsSource(
                    settings_cls,
                    json_file=path,
                ),
                file_secret_settings,
            )

    return JsonSettings()



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
    database: DatabaseSettings = DatabaseSettings()


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


