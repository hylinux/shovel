from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from shovel.config.paths import resolve_root_path


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_path: Path = Field(default_factory=lambda: resolve_root_path() / "workspaces" / "default")
    enable_python_extensions: bool = False
    max_tool_iterations: int = Field(default=10, ge=1, le=100)

    @property
    def workspace(self) -> Path:
        return self.workspace_path.expanduser().resolve()
