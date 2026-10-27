"""Classic Outlook automation. Run in its own interactive-user STA process."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from shovel.services.outlook import (
    OutlookAttachment,
    OutlookBatch,
    OutlookConfig,
    OutlookFolder,
    OutlookItem,
    OutlookMail,
    OutlookStore,
    WorkerResponse,
)


def _timestamp(value: datetime | None) -> int | None:
    return int(value.timestamp()) if value is not None else None


def _version(entry_id: str, modified: datetime, size: int) -> str:
    # Outlook table strings can be truncated; do not compare a table subject with a full MailItem subject.
    value = [entry_id, modified.timestamp(), size]
    return hashlib.sha256(json.dumps(value, ensure_ascii=True).encode()).hexdigest()


class OutlookReader:
    def __init__(self, namespace: Any) -> None:
        self.namespace = namespace
        self._pending: Iterator[OutlookItem] | None = None
        self._authorized: dict[tuple[str, str], OutlookItem] = {}
        self._config: OutlookConfig | None = None

    def close(self) -> None:
        # Break the suspended discovery generator's cycle before uninitializing COM.
        self._pending = None
        self._authorized.clear()
        self._config = None
        self.namespace = None

    def _folder(self, store_id: str, folder_id: str) -> Any:
        folder = self.namespace.GetFolderFromID(folder_id, store_id)
        if str(folder.StoreID) != store_id:
            raise ValueError("Outlook returned a folder from a different store.")
        return folder

    def _folders(self, config: OutlookConfig) -> Iterator[Any]:
        seen: set[str] = set()
        stack = list(reversed(config.folder_ids))
        while stack:
            identifier = stack.pop()
            if identifier in seen:
                continue
            seen.add(identifier)
            folder = self._folder(config.store_id, identifier)
            yield folder
            if config.include_subfolders:
                stack.extend(str(child.EntryID) for child in folder.Folders)

    def _discover(self, config: OutlookConfig) -> Iterator[OutlookItem]:
        for folder in self._folders(config):
            table = folder.GetTable()
            table.Columns.RemoveAll()
            for name in (
                "EntryID",
                "Subject",
                "LastModificationTime",
                "Size",
                "MessageClass",
                "ReceivedTime",
            ):
                table.Columns.Add(name)
            while not table.EndOfTable:
                row = table.GetNextRow()
                message_class = str(row["MessageClass"] or "")
                if message_class != "IPM.Note" and not message_class.startswith("IPM.Note."):
                    continue
                entry_id = str(row["EntryID"])
                subject = str(row["Subject"] or "")
                modified = row["LastModificationTime"]
                size = int(row["Size"])
                yield OutlookItem(
                    store_id=config.store_id,
                    entry_id=entry_id,
                    folder_id=str(folder.EntryID),
                    folder_path=str(folder.FolderPath),
                    subject=subject,
                    modified_at=int(modified.timestamp()),
                    received_at=_timestamp(row["ReceivedTime"]),
                    size_bytes=size,
                    version=_version(entry_id, modified, size),
                )

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request["command"]
        if command == "stores":
            return {
                "items": [
                    OutlookStore(id=str(store.StoreID), name=str(store.DisplayName)).model_dump()
                    for store in self.namespace.Stores
                ]
            }
        if command == "folders":
            store_id = str(request["store_id"])
            parent_id = request.get("parent_id")
            if parent_id:
                parent = self._folder(store_id, str(parent_id))
            else:
                parent = self.namespace.GetStoreFromID(store_id).GetRootFolder()
            return {
                "items": [
                    OutlookFolder(
                        id=str(folder.EntryID), name=str(folder.Name), path=str(folder.FolderPath)
                    ).model_dump()
                    for folder in parent.Folders
                ]
            }
        if command == "verify":
            config = OutlookConfig.model_validate(request["config"])
            folders = list(self._folders(config))
            for folder in folders:
                table = folder.GetTable()
                _ = table.EndOfTable
            return {"folders": len(folders)}
        if command == "discover":
            self._config = OutlookConfig.model_validate(request["config"])
            self._authorized.clear()
            self._pending = self._discover(self._config)
            return self._batch()
        if command == "next":
            return self._batch()
        if command == "open":
            return self._open(str(request["store_id"]), str(request["entry_id"]), str(request["version"]))
        raise ValueError(f"Unsupported Outlook worker command: {command}")

    def _batch(self) -> dict[str, Any]:
        if self._pending is None:
            raise ValueError("No Outlook discovery is active.")
        items: list[OutlookItem] = []
        done = False
        for _ in range(100):
            item = next(self._pending, None)
            if item is None:
                done = True
                break
            self._authorized[(item.store_id, item.entry_id)] = item
            items.append(item)
        return OutlookBatch(items=items, done=done).model_dump()

    def _open(self, store_id: str, entry_id: str, version: str) -> dict[str, Any]:
        discovered = self._authorized.get((store_id, entry_id))
        if discovered is None or self._config is None or discovered.version != version:
            raise ValueError("Mail was not discovered in this scan's authorized folders.")
        item = self.namespace.GetItemFromID(entry_id, store_id)
        parent = item.Parent
        if str(parent.EntryID) != discovered.folder_id or str(parent.StoreID) != store_id:
            raise ValueError("Mail moved after discovery; scan again.")
        before = _version(str(item.EntryID), item.LastModificationTime, int(item.Size))
        if before != version:
            raise ValueError("Mail changed after discovery; scan again.")
        if int(item.DownloadState) != 1:
            raise ValueError(
                "Only mail headers are cached. Download the full message in Outlook, then retry."
            )
        body = str(item.Body or "")
        if not body.strip():
            raise ValueError("Mail has no accessible text body; protected or empty mail is not indexed.")
        limit = self._config.max_body_chars
        mail = OutlookMail(
            store_id=store_id,
            entry_id=entry_id,
            version=version,
            subject=str(item.Subject or ""),
            sender=f"{item.SenderName} <{item.SenderEmailAddress}>",
            to=str(item.To or ""),
            cc=str(item.CC or ""),
            sent_at=_timestamp(item.SentOn),
            received_at=_timestamp(item.ReceivedTime),
            folder_path=discovered.folder_path,
            thread_id=str(item.ConversationID or "") or None,
            body=body[:limit],
            truncated=len(body) > limit,
            attachments=[
                OutlookAttachment(name=str(attachment.FileName), size_bytes=int(attachment.Size))
                for attachment in item.Attachments
            ],
        )
        after = _version(str(item.EntryID), item.LastModificationTime, int(item.Size))
        if after != before:
            raise ValueError("Mail changed while reading; scan again.")
        return mail.model_dump()


def main() -> None:
    pythoncom: Any = None
    initialized = False
    reader: OutlookReader | None = None
    try:
        if sys.platform != "win32":
            raise RuntimeError("Local Outlook access requires Windows.")
        pythoncom = importlib.import_module("pythoncom")
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        initialized = True
        client = importlib.import_module("win32com.client")
        reader = OutlookReader(client.Dispatch("Outlook.Application").GetNamespace("MAPI"))
        for line in sys.stdin.buffer:
            try:
                request = json.loads(line)
                response = WorkerResponse(result=reader.handle(request))
            except Exception as exc:
                # This is the process boundary: failures must reach the owning scan, not disappear.
                response = WorkerResponse(error=f"Outlook {type(exc).__name__}: {exc}")
            sys.stdout.buffer.write(response.model_dump_json().encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()
    except Exception as exc:
        response = WorkerResponse(error=f"Cannot initialize classic Outlook: {exc}", fatal=True)
        sys.stdout.buffer.write(response.model_dump_json().encode("utf-8") + b"\n")
        sys.stdout.buffer.flush()
    finally:
        if reader is not None:
            reader.close()
        reader = None
        if initialized:
            pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
