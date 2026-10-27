"""Read-only, classic Outlook resource backed by a scan-owned STA worker."""

from __future__ import annotations

import fnmatch
import time
from collections.abc import AsyncIterator
from typing import ClassVar

from pydantic import BaseModel, SecretStr

from shovel.domain.enums import DocClass, Modality, Sensitivity
from shovel.exceptions.pipeline import ParseError
from shovel.exceptions.resource import ConnectorDiscoveryError
from shovel.services.connector_base import (
    ConnectorCapability,
    ConnectorSpec,
    DiscoveredItem,
    DiscoveryScope,
    VerifyResult,
)
from shovel.services.outlook import (
    MAIL_FORMAT,
    OutlookBatch,
    OutlookConfig,
    OutlookError,
    OutlookItem,
    OutlookMail,
    OutlookSession,
    OutlookWorkerError,
)


class OutlookConnector:
    spec: ClassVar[ConnectorSpec] = ConnectorSpec(
        kind="outlook_local",
        display_name="Local Outlook mail",
        description="Read mail from explicitly selected classic Outlook folders on Windows.",
        config_model=OutlookConfig,
        capabilities=frozenset(
            {
                ConnectorCapability.DISCOVER,
                ConnectorCapability.INCREMENTAL,
                ConnectorCapability.RANDOM_READ,
            }
        ),
        default_sensitivity=Sensitivity.PERSONAL,
    )

    def __init__(self, config: BaseModel, credential: SecretStr | None = None) -> None:
        if not isinstance(config, OutlookConfig):
            raise TypeError("OutlookConnector requires OutlookConfig.")
        self.config = config
        self._session = OutlookSession(config.timeout_seconds)
        self._items: dict[str, OutlookItem] = {}

    async def aclose(self) -> None:
        self._items.clear()
        await self._session.aclose()

    async def verify(self) -> VerifyResult:
        try:
            result = await self._session.request("verify", config=self.config.model_dump())
            return VerifyResult.success(
                "Classic Outlook folders are readable. "
                "Deleted mail is not automatically removed from Shovel.",
                details=result,
            )
        except OutlookError as exc:
            return VerifyResult.failure(f"{exc.message} {exc.hint}")
        finally:
            await self.aclose()

    async def discover(self, scope: DiscoveryScope) -> AsyncIterator[DiscoveredItem]:
        self._items.clear()
        cutoff = time.time() - scope.time_window_days * 86400 if scope.time_window_days is not None else None
        try:
            result = await self._session.request("discover", config=self.config.model_dump())
            while True:
                batch = OutlookBatch.model_validate(result)
                for item in batch.items:
                    if item.store_id != self.config.store_id:
                        raise OutlookError("Outlook returned mail from an unauthorized store.")
                    identifier = item.external_id
                    if identifier in self._items:
                        continue
                    if scope.target_ids and identifier not in scope.target_ids:
                        continue
                    path = f"{item.folder_path}\\{item.subject}"
                    if scope.include and not any(fnmatch.fnmatchcase(path, p) for p in scope.include):
                        continue
                    if any(fnmatch.fnmatchcase(path, p) for p in scope.exclude):
                        continue
                    if scope.max_file_bytes is not None and item.size_bytes > scope.max_file_bytes:
                        continue
                    if cutoff is not None and (item.received_at is None or item.received_at < cutoff):
                        continue
                    self._items[identifier] = item
                    yield DiscoveredItem(
                        external_id=identifier,
                        uri=f"outlook-local://mail/{identifier}",
                        title=item.subject or "(No subject)",
                        size_bytes=item.size_bytes,
                        modified_at=item.modified_at,
                        content_hash=item.version,
                        doc_class=DocClass.EMAIL,
                        modality=Modality.TEXT,
                        extra={
                            "content_format": MAIL_FORMAT,
                            "folder_path": item.folder_path,
                            "received_at": item.received_at,
                        },
                    )
                if batch.done:
                    break
                result = await self._session.request("next")
        except OutlookError as exc:
            raise ConnectorDiscoveryError("classic Outlook", exc.message) from exc

    async def open(self, item: DiscoveredItem) -> AsyncIterator[bytes]:
        discovered = self._items.get(item.external_id)
        if discovered is None or item.content_hash != discovered.version:
            raise ParseError(item.uri, "Mail was not discovered by this connector instance.")
        try:
            result = await self._session.request(
                "open",
                store_id=discovered.store_id,
                entry_id=discovered.entry_id,
                version=discovered.version,
            )
            mail = OutlookMail.model_validate(result)
            if (
                mail.store_id != discovered.store_id
                or mail.entry_id != discovered.entry_id
                or mail.version != discovered.version
            ):
                raise OutlookError("Outlook returned a different mail revision.")
            yield mail.model_dump_json().encode()
        except OutlookWorkerError as exc:
            raise ConnectorDiscoveryError("classic Outlook session", exc.message) from exc
        except OutlookError as exc:
            raise ParseError(item.uri, exc.message) from exc
