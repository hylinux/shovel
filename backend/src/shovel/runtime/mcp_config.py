from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from shovel.runtime.agent_extensions import MCPSettings, mcp_revision
from shovel.runtime.errors import RuntimeFailure


class MCPConfigurationService:
    def __init__(self, workspace: Path) -> None:
        self.path = workspace / "mcp.json"
        self._lock = threading.Lock()

    @staticmethod
    def _validate(content: str) -> MCPSettings:
        try:
            return MCPSettings.model_validate_json(content)
        except ValidationError as exc:
            raise RuntimeFailure(
                "MCP 配置无效,请检查 JSON、字段类型以及 command/url/type 是否匹配。",
                status=422,
            ) from exc

    def _read(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def _view(self, content: bytes | None) -> dict[str, Any]:
        text = content.decode("utf-8") if content is not None else '{\n  "mcpServers": {}\n}\n'
        error = None
        items = []
        try:
            config = self._validate(text)
        except RuntimeFailure as exc:
            error = exc.message
        else:
            items = [
                {
                    "name": name,
                    "transport": "stdio" if server.command else "http",
                    "enabled": not server.disabled,
                }
                for name, server in config.mcpServers.items()
            ]
        return {
            "path": str(self.path),
            "content": text,
            "revision": mcp_revision(content),
            "exists": content is not None,
            "items": items,
            "validation_error": error,
        }

    def read(self) -> dict[str, Any]:
        try:
            with self._lock:
                return self._view(self._read())
        except (OSError, UnicodeError) as exc:
            raise RuntimeFailure("无法读取工作区 mcp.json,请检查文件权限和 UTF-8 编码。", status=503) from exc

    def save(self, content: str, revision: str) -> dict[str, Any]:
        self._validate(content)
        encoded = content.encode("utf-8")
        try:
            with self._lock:
                if mcp_revision(self._read()) != revision:
                    raise RuntimeFailure(
                        "mcp.json 已被修改。请重新加载文件,合并修改后再保存。",
                        status=409,
                    )
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=self.path.parent, prefix=".mcp-", suffix=".tmp", delete=False
                    ) as file:
                        temporary = Path(file.name)
                        file.write(encoded)
                        file.flush()
                        os.fsync(file.fileno())
                    if mcp_revision(self._read()) != revision:
                        raise RuntimeFailure("mcp.json 已被修改,请重新加载后再保存。", status=409)
                    os.replace(temporary, self.path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                return self._view(encoded)
        except (OSError, UnicodeError) as exc:
            raise RuntimeFailure("无法保存工作区 mcp.json,请检查文件权限和磁盘空间。", status=503) from exc
