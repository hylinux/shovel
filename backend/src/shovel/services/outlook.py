"""Versioned Outlook data and a bounded, task-owned COM subprocess transport."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sys
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from shovel.exceptions.resource import ResourceError

MAIL_FORMAT: Literal["outlook-mail-v1"] = "outlook-mail-v1"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class OutlookError(ResourceError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            hint=(
                "Use classic Outlook in the current Windows user session. "
                "Install `uv sync --extra outlook`, open Outlook and check its profile, "
                "permissions and any security dialogs, then retry. New Outlook is not supported."
            ),
        )


class OutlookWorkerError(OutlookError):
    """Transport/session failure: abort the scan rather than failing every message."""


class OutlookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store_id: str = Field(min_length=1, max_length=4096, description="Outlook StoreID, not its display name.")
    folder_ids: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        min_length=1, max_length=100, description="Explicitly authorized FolderIDs."
    )
    include_subfolders: bool = False
    timeout_seconds: int = Field(default=60, ge=5, le=300)
    max_body_chars: int = Field(default=1_000_000, ge=1000, le=4_000_000)


class OutlookAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    size_bytes: int = Field(ge=0)


class OutlookMail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["outlook-mail-v1"] = MAIL_FORMAT
    store_id: str
    entry_id: str
    version: str
    subject: str
    sender: str
    to: str
    cc: str
    sent_at: int | None
    received_at: int | None
    folder_path: str
    thread_id: str | None
    body: str
    truncated: bool = False
    attachments: list[OutlookAttachment] = Field(default_factory=list)


class OutlookItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str
    entry_id: str
    folder_id: str
    folder_path: str
    subject: str
    modified_at: int
    received_at: int | None
    size_bytes: int = Field(ge=0)
    version: str

    @property
    def external_id(self) -> str:
        return hashlib.sha256(json.dumps([self.store_id, self.entry_id]).encode()).hexdigest()


class OutlookStore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str


class OutlookFolder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    path: str


class OutlookBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[OutlookItem]
    done: bool


class WorkerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: dict[str, Any] | None = None
    error: str | None = None
    fatal: bool = False


class OutlookSession:
    """Only this subprocess touches COM; no Outlook object crosses the boundary."""

    def __init__(self, timeout: int = 60) -> None:
        self.timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> OutlookSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def request(self, command: str, **payload: object) -> dict[str, Any]:
        async with self._lock:
            try:
                async with asyncio.timeout(self.timeout):
                    if self._process is None:
                        await self._start()
                    process = self._process
                    assert process is not None and process.stdin is not None and process.stdout is not None
                    process.stdin.write(
                        (json.dumps({"command": command, **payload}, ensure_ascii=True) + "\n").encode()
                    )
                    await process.stdin.drain()
                    line = await process.stdout.readline()
                    if not line:
                        raise OutlookWorkerError("The Outlook worker exited before returning a result.")
                    response = WorkerResponse.model_validate_json(line)
                    if response.error is not None:
                        if response.fatal:
                            raise OutlookWorkerError(response.error)
                        raise OutlookError(response.error)
                    if response.result is None:
                        raise OutlookWorkerError("The Outlook worker returned no result.")
                    return response.result
            except TimeoutError as exc:
                await self.aclose()
                raise OutlookWorkerError(f"Outlook did not respond within {self.timeout} seconds.") from exc
            except (OSError, ValueError) as exc:
                await self.aclose()
                raise OutlookWorkerError("Invalid or interrupted Outlook worker communication.") from exc
            except OutlookWorkerError:
                await self.aclose()
                raise
            except asyncio.CancelledError:
                await self.aclose()
                raise

    async def _start(self) -> None:
        if sys.platform != "win32":
            raise OutlookWorkerError("Local Outlook access requires Windows.")
        start = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "shovel.services.outlook_worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_RESPONSE_BYTES,
            )
        )
        try:
            self._process = await asyncio.shield(start)
        except asyncio.CancelledError:
            self._process = await start
            raise

    async def aclose(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            async with asyncio.timeout(2):
                await process.wait()
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def outlook_stores() -> list[OutlookStore]:
    async with OutlookSession() as session:
        result = await session.request("stores")
        return [OutlookStore.model_validate(item) for item in result["items"]]


async def outlook_folders(store_id: str, parent_id: str | None = None) -> list[OutlookFolder]:
    async with OutlookSession() as session:
        result = await session.request("folders", store_id=store_id, parent_id=parent_id)
        return [OutlookFolder.model_validate(item) for item in result["items"]]
