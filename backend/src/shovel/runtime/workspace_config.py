from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from pathlib import Path
from typing import TypedDict

from shovel.runtime.errors import RuntimeFailure


def content_revision(content: bytes | None) -> str:
    return hashlib.sha256(b"missing" if content is None else b"file:" + content).hexdigest()


class WorkspaceTextView(TypedDict):
    path: str
    content: str
    revision: str
    exists: bool


class WorkspaceTextFile:
    def __init__(self, path: Path, *, default: str = "") -> None:
        self.path, self.default = path, default
        self._lock = threading.Lock()

    def _read(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def _view(self, content: bytes | None) -> WorkspaceTextView:
        return {
            "path": str(self.path),
            "content": content.decode("utf-8") if content is not None else self.default,
            "revision": content_revision(content),
            "exists": content is not None,
        }

    def read(self) -> WorkspaceTextView:
        try:
            with self._lock:
                return self._view(self._read())
        except (OSError, UnicodeError) as exc:
            raise RuntimeFailure(
                f"无法读取工作区 {self.path.name},请检查文件权限和 UTF-8 编码。", status=503
            ) from exc

    def save(self, content: str, revision: str) -> WorkspaceTextView:
        try:
            encoded = content.encode("utf-8")
            with self._lock:
                if content_revision(self._read()) != revision:
                    raise RuntimeFailure(
                        f"{self.path.name} 已被修改。请重新加载文件,合并修改后再保存。", status=409
                    )
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=self.path.parent, prefix=f".{self.path.stem}-", suffix=".tmp", delete=False
                    ) as file:
                        temporary = Path(file.name)
                        file.write(encoded)
                        file.flush()
                        os.fsync(file.fileno())
                    if content_revision(self._read()) != revision:
                        raise RuntimeFailure(f"{self.path.name} 已被修改,请重新加载后再保存。", status=409)
                    os.replace(temporary, self.path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                return self._view(encoded)
        except (OSError, UnicodeError) as exc:
            raise RuntimeFailure(
                f"无法保存工作区 {self.path.name},请检查文件权限和磁盘空间。", status=503
            ) from exc
