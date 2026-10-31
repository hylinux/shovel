from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PerformanceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warm_index: bool = True
    warm_local_models: bool = True
    max_foreground_runs: int = Field(default=0, ge=0, le=64)
    foreground_queue_seconds: float = Field(default=120, gt=0, le=600)
    background_idle_seconds: float = Field(default=5, ge=0, le=300)
    embedding_cache_entries: int = Field(default=128, ge=0, le=4096)
    embedding_cache_seconds: float = Field(default=300, gt=0, le=3600)
    max_local_model_requests: int = Field(default=1, ge=1, le=64)
    context_profile: Literal["default", "compact"] = "default"
