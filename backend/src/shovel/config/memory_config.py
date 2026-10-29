"""Native memory: SQLite authority and an independently configured derived index."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shovel.config.embedding_config import EmbeddingSettings
from shovel.config.model_settings import DefaultAgentModelSettings


class MemoryModelSettings(DefaultAgentModelSettings):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def complete_override(self) -> MemoryModelSettings:
        if (
            any(
                (
                    self.model_provider_type,
                    self.model_name,
                    self.security_key,
                    self.base_endpoint,
                    self.api_version,
                )
            )
            and not self.is_configured
        ):
            raise ValueError("[memory.llm] requires both model_provider_type and model_name.")
        return self


class MemorySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection: str = "shovel_memory"
    index_path: Path = Field(default_factory=lambda: Path.home() / ".shovel" / "memory" / "zvec")
    dim: int = Field(default=1024, ge=1)
    context_max_chars: int = Field(default=2000, ge=100, le=32000)
    max_items: int = Field(default=10, ge=1, le=100)
    min_similarity: float = Field(default=0.55, ge=0, le=1)
    min_confidence: float = Field(default=0.8, ge=0, le=1)
    summary_max_chars: int = Field(default=2000, ge=100, le=8000)
    history_messages: int = Field(default=20, ge=4, le=100)
    embedder: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    # An unset model follows [model]; this client has its own lifecycle.
    llm: MemoryModelSettings = Field(default_factory=MemoryModelSettings)
