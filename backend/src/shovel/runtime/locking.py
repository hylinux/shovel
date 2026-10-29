from __future__ import annotations

import sys
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from sqlalchemy.engine import make_url

from shovel.config.settings import AppSettings
from shovel.runtime.errors import RuntimeFailure


def endpoint_file(settings: AppSettings) -> Path:
    target = Path(settings.zvec.knowledge_path).expanduser().resolve()
    return target.with_name(target.name + ".runtime.json")


class RuntimeLock:
    """Lock both SQLite and Zvec identities; OS releases locks on process death."""

    def __init__(self, settings: AppSettings) -> None:
        vector_path = Path(settings.zvec.knowledge_path).expanduser().resolve()
        paths = {vector_path.with_name(vector_path.name + ".runtime.lock")}
        memory_path = settings.memory.index_path.expanduser().resolve()
        paths.add(memory_path.with_name(memory_path.name + ".runtime.lock"))
        database = make_url(settings.database.url).database
        if database and database != ":memory:":
            paths.add(Path(database).expanduser().resolve().with_suffix(".runtime.lock"))
        self._paths = sorted(paths)
        self._handles: list[BinaryIO] = []

    def __enter__(self) -> RuntimeLock:
        try:
            for path in self._paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("a+b")
                self._handles.append(handle)
                if path.stat().st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    if sys.platform == "win32":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    # The unsuccessful handle is not owned and must not be unlocked.
                    self._handles.pop().close()
                    raise RuntimeFailure(
                        "此数据目录已有 Shovel Host 正在运行。",
                        status=409,
                        hint="通过运行中的 Host 提交操作,不要同时启动第二个写入进程。",
                    ) from exc
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        for handle in reversed(self._handles):
            try:
                handle.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        self._handles.clear()
