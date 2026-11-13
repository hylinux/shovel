from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from shovel.runtime.agent_extensions import MCPSettings
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.workspace_config import WorkspaceTextFile, WorkspaceTextView


class MCPConfigurationService:
    def __init__(self, workspace: Path) -> None:
        self.path = workspace / "mcp.json"
        self._file = WorkspaceTextFile(self.path, default='{\n  "mcpServers": {}\n}\n')

    @staticmethod
    def _validate(content: str) -> MCPSettings:
        try:
            return MCPSettings.model_validate_json(content)
        except ValidationError as exc:
            raise RuntimeFailure(
                "MCP 配置无效,请检查 JSON、字段类型以及 command/url/type 是否匹配。",
                status=422,
            ) from exc

    def _view(self, view: WorkspaceTextView) -> dict[str, Any]:
        error = None
        items = []
        warnings = []
        try:
            config = self._validate(view["content"])
        except RuntimeFailure as exc:
            error = exc.message
        else:
            items = [
                {
                    "name": name,
                    "transport": "stdio" if server.command else "http",
                    "enabled": not server.disabled,
                    "auth_type": (
                        server.auth.type
                        if server.auth is not None
                        else "stdio_credentials"
                        if server.env_credentials
                        else "legacy"
                        if server.headers or server.env
                        else "none"
                    ),
                    "credential_refs": server.credential_references(),
                }
                for name, server in config.mcpServers.items()
            ]
            if any(server.headers or server.env for server in config.mcpServers.values()):
                warnings.append(
                    "Legacy headers/env are saved and returned verbatim. "
                    "Use auth/env_credentials references for secrets; existing values were not migrated."
                )
        return {
            **view,
            "items": items,
            "validation_error": error,
            "warnings": warnings,
        }

    def read(self) -> dict[str, Any]:
        return self._view(self._file.read())

    def save(self, content: str, revision: str) -> dict[str, Any]:
        self._validate(content)
        return self._view(self._file.save(content, revision))
