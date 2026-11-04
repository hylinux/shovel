"""Fixed, table-scoped Kusto management metadata; no query/data/CLI fallback."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable

import httpx
from pydantic import Field

from shovel.enterprise.domain import EnterpriseResourceRef, EnterpriseValue
from shovel.enterprise.organization import MetadataConnection, unique_json
from shovel.identity.domain import AuthContext, TokenRequest, UnixSeconds
from shovel.protection.domain import ProtectionAdapterError, ProtectionDeniedError, content_digest


class KustoSourceDenied(ProtectionDeniedError):
    def __init__(self, status: int) -> None:
        super().__init__("Kusto metadata authorization is unavailable.")
        self.status = status


class KustoObservation(EnterpriseValue):
    database_version: str
    columns: tuple[tuple[str, str], ...]
    present: bool = Field(strict=True)
    observed_at: UnixSeconds

    @property
    def version(self) -> str:
        return content_digest(
            {"database_version": self.database_version, "columns": self.columns, "present": self.present}
        )

    def body(self) -> bytes:
        return json.dumps(self.model_dump(mode="json", exclude={"observed_at"}), sort_keys=True).encode()


def metadata_resource(connection: MetadataConnection, version: str) -> EnterpriseResourceRef:
    return EnterpriseResourceRef(
        provider_id=connection.profile.provider_id,
        connection_profile_id=connection.profile.id,
        tenant_id=connection.profile.tenant_binding,
        resource_type="kusto.table_metadata",
        resource_id=content_digest(
            {
                "endpoint": connection.profile.endpoint,
                "database": connection.database,
                "table": connection.table,
            }
        ),
        source_version=version,
    )


def metadata_command(connection: MetadataConnection) -> str:
    database = json.dumps(connection.database, ensure_ascii=True)
    table = json.dumps(connection.table, ensure_ascii=True)
    return (
        f".show database [{database}] schema "
        f"| where TableName == {table} or isempty(TableName) "
        "| project DatabaseName, TableName, ColumnName, ColumnType, Version"
    )


def parse_metadata(data: bytes, connection: MetadataConnection, observed_at: float) -> KustoObservation:
    try:
        root = unique_json(data)
        if not isinstance(root, dict) or set(root) != {"Tables"} or len(root["Tables"]) != 1:
            raise ValueError("Incomplete metadata result.")
        table = root["Tables"][0]
        fields = ["DatabaseName", "TableName", "ColumnName", "ColumnType", "Version"]
        if [item["ColumnName"] for item in table["Columns"]] != fields:
            raise ValueError("Unexpected metadata columns.")
        rows = table["Rows"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 4096:
            raise ValueError("Metadata result is unsupported.")
        versions, columns, present = [], [], False
        for row in rows:
            if (
                not isinstance(row, list)
                or len(row) != 5
                or any(
                    value is not None and (not isinstance(value, str) or len(value) > 1024) for value in row
                )
                or row[0] != connection.database
            ):
                raise ValueError("Metadata result is differently scoped.")
            row = [value if value is not None else "" for value in row]
            if row[1] == "":
                if row[2] or row[3] or not re.fullmatch(r"v\.?\d+\.\d+", row[4]):
                    raise ValueError("Metadata database version unavailable.")
                versions.append(row[4])
            elif row[1] == connection.table:
                present = True
                if row[2]:
                    if not row[3]:
                        raise ValueError("Metadata column type unavailable.")
                    columns.append((row[2], row[3]))
            else:
                raise ValueError("Metadata result exceeded the allowed table.")
        if len(versions) != 1 or len({column[0] for column in columns}) != len(columns):
            raise ValueError("Metadata version or schema is ambiguous.")
        if present and not columns:
            raise ValueError("Incomplete table metadata.")
        return KustoObservation(
            database_version=versions[0],
            columns=tuple(sorted(columns)),
            present=present,
            observed_at=observed_at,
        )
    except Exception:
        raise ProtectionAdapterError("Kusto metadata response is invalid, partial or unsupported.") from None


class KustoMetadataClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.transport, self.clock = transport, clock

    async def observe(
        self, connection: MetadataConnection, request: TokenRequest, credential: AuthContext
    ) -> KustoObservation:
        if (
            request.model_copy(
                update={
                    name: getattr(connection.authentication, name)
                    for name in ("request_id", "execution_id", "correlation_id")
                }
            )
            != connection.authentication
        ):
            raise ProtectionDeniedError("Kusto metadata authentication binding changed.")
        started = self.clock()
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(10),
            ) as client:
                outgoing = credential.consume(
                    request,
                    now=self.clock(),
                    transport=lambda secret: client.build_request(
                        "POST",
                        connection.profile.endpoint + "/v1/rest/mgmt",
                        headers={"Authorization": "Bearer " + secret.get_secret_value()},
                        json={"db": connection.database, "csl": metadata_command(connection)},
                    ),
                )
                response = await client.send(outgoing, stream=True)
                try:
                    if response.status_code in {401, 403}:
                        raise KustoSourceDenied(response.status_code)
                    if response.status_code != 200:
                        raise ProtectionAdapterError("Kusto metadata request did not complete successfully.")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 262144:
                            raise ProtectionAdapterError("Kusto metadata response exceeded the size limit.")
                    return parse_metadata(bytes(data), connection, started)
                finally:
                    await response.aclose()
        except httpx.HTTPError:
            raise ProtectionAdapterError("Kusto metadata transport is unavailable.") from None
        finally:
            credential.close()
