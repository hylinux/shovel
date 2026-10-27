from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from shovel.config.settings import AppSettings
from shovel.db.base import new_id
from shovel.db.database import create_async_database_engine, create_async_session_factory
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.hosting import build_cli_host, local_endpoint, resolve_runtime
from shovel.runtime.locking import endpoint_file
from shovel.runtime.operations import TERMINAL
from shovel.runtime.requests import request_operation_id
from shovel.runtime.schemas import CleanupInput, HistoryPruneInput, ProfileInput, ResourceInput, ScanInput
from shovel.runtime.service import Runtime
from shovel.services.credentials import CredentialRef, build_credential_store


def workspace_id(settings: AppSettings) -> str:
    identity = json.dumps(
        [
            settings.database.url,
            str(Path(settings.zvec.knowledge_path).expanduser().resolve()),
        ]
    )
    return hashlib.sha256(identity.encode()).hexdigest()


async def save_credential(secret: SecretStr, identity_ref: str | None = None) -> str:
    reference = CredentialRef.parse(identity_ref or f"keyring:{new_id('credential')}")
    if not reference.is_writable:
        raise RuntimeFailure("不能将密钥写入只读的环境变量引用。")
    await asyncio.to_thread(build_credential_store().set, reference, secret)
    return str(reference)


async def _remote(settings: AppSettings, client: httpx.AsyncClient) -> str | None:
    metadata = endpoint_file(settings)
    if not metadata.is_file():
        return None
    try:
        data = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeFailure(f"无法读取 Host 发现文件: {metadata}") from exc
    url = data.get("url", "")
    if not isinstance(url, str) or not local_endpoint(url):
        raise RuntimeFailure("Host 发现文件包含无效的本机地址。")
    try:
        response = await client.get(f"{url}/api/v1/status", timeout=1)
    except httpx.ConnectError, httpx.TimeoutException:
        # A dead Host may leave a receipt. The one-shot Host still must acquire OS locks.
        return None
    if response.status_code != 200:
        return None
    try:
        status = response.json()
    except ValueError:
        return None
    if (
        not isinstance(status, dict)
        or status.get("service") != "shovel"
        or status.get("workspace_id") != workspace_id(settings)
    ):
        return None
    return url


def _decode(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeFailure("Host 返回了无效的 JSON 响应。", status=502) from exc
    if not isinstance(data, dict):
        raise RuntimeFailure("Host 返回了不支持的操作结果。", status=502)
    if response.is_error:
        error = data.get("error") or {}
        if not isinstance(error, dict):
            raise RuntimeFailure(f"Host 返回 HTTP {response.status_code}。", status=response.status_code)
        raise RuntimeFailure(
            error.get("message") or f"Host 返回 HTTP {response.status_code}。",
            hint=error.get("hint"),
            status=response.status_code,
        )
    return data


def _decode_operation(response: httpx.Response) -> dict[str, Any]:
    data = _decode(response)
    if (
        not isinstance(data.get("id"), str)
        or data.get("state") not in (*TERMINAL, "queued", "running")
        or not isinstance(data.get("result", {}), dict)
    ):
        raise RuntimeFailure("Host 返回了无效的操作回执。", status=502)
    return data


async def _submit_local(
    runtime: Runtime,
    kind: str,
    payload: dict[str, Any],
    ref: str | None,
    request_key: str,
) -> str:
    if kind == "resource.create":
        operation = await runtime.operations.create_resource(
            ResourceInput.model_validate(payload), request_key=request_key
        )
    elif kind == "scan":
        operation = await runtime.operations.scan(
            ref or "", ScanInput.model_validate(payload), request_key=request_key
        )
    elif kind in {"cleanup", "history.prune"}:
        maintenance = (
            CleanupInput.model_validate(payload)
            if kind == "cleanup"
            else HistoryPruneInput.model_validate(payload)
        )
        operation = await runtime.operations.enqueue(kind, maintenance.model_dump(), request_key=request_key)
    elif kind.startswith("profile."):
        if not isinstance(payload.get("name"), str):
            raise RuntimeFailure("Profile 操作必须指定 name。")
        if kind == "profile.save":
            profile = ProfileInput.model_validate(payload.get("profile")).model_dump(exclude_none=True)
        elif kind == "profile.delete" and payload.get("profile") is None:
            profile = None
        else:
            raise RuntimeFailure("Profile 操作类型或参数不合法。")
        operation = await runtime.operations.profile_action(
            ref or "",
            payload["name"],
            profile,
            request_key=request_key,
        )
    elif kind.startswith("resource."):
        operation = await runtime.operations.resource_action(
            ref or "", kind.removeprefix("resource."), payload, request_key=request_key
        )
    else:
        raise RuntimeFailure(f"不支持的 CLI 操作: {kind}")
    return operation.id


def _record_request(settings: AppSettings, key: str, kind: str, ref: str | None) -> Path:
    directory = endpoint_file(settings).parent / "requests"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{request_operation_id(key)}.json"
    temporary = directory / f"{new_id('receipt')}.tmp"
    try:
        temporary.write_text(
            json.dumps({"id": request_operation_id(key), "request_key": key, "kind": kind, "ref": ref}),
            encoding="utf-8",
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _disconnected(identifier: str, key: str | None = None) -> RuntimeFailure:
    return RuntimeFailure(
        f"Host 连接中断,操作执行结果未知。操作 ID: {identifier}",
        hint=(
            f"运行 `shovel operation wait {identifier}` 找回结果。"
            + (f" 若需重发原请求,使用相同的 --request-key {key},不要换键重复提交。" if key else "")
        ),
        status=503,
    )


def _check_result(data: dict[str, Any]) -> dict[str, Any]:
    if data["state"] in {"failed", "cancelled", "completed_with_errors"}:
        message = (
            data.get("error")
            or data.get("result", {}).get("error_detail")
            or data.get("result", {}).get("message")
            or f"后台操作未完整成功: {data['state']}。"
        )
        if data.get("result", {}).get("metadata_deleted"):
            message = f"资源元数据已删除,但向量清理未完成:{message}"
        if data.get("result", {}).get("profile_deleted"):
            message = f"Profile 已删除,但向量清理未完成:{message}"
        raise RuntimeFailure(message, hint=f"操作 {data['id']};可在 Web 重试或运行 shovel scan cleanup。")
    return data


async def execute_command(
    settings: AppSettings,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    *,
    ref: str | None = None,
    secret: SecretStr | None = None,
    request_key: str | None = None,
) -> dict[str, Any]:
    from shovel.runtime.api import operation_view

    body = dict(payload or {})
    if kind == "cleanup":
        body = CleanupInput.model_validate(body).model_dump()
    elif kind == "history.prune":
        body = HistoryPruneInput.model_validate(body).model_dump()
    key = request_key or new_id("request")
    identifier = request_operation_id(key)
    await asyncio.to_thread(_record_request, settings, key, kind, ref)
    if secret is not None and not body.get("identity_ref"):
        body["identity_ref"] = f"keyring:credential_{identifier}"
    async with httpx.AsyncClient(timeout=30) as client:
        url = await _remote(settings, client)
        if url is not None:
            try:
                if secret is not None:
                    staged = _decode(
                        await client.post(
                            f"{url}/api/v1/credentials",
                            json={
                                "secret": secret.get_secret_value(),
                                "identity_ref": body.get("identity_ref"),
                            },
                        )
                    )
                    body["identity_ref"] = staged["identity_ref"]
                encoded = quote(ref or "", safe="")
                paths = {
                    "resource.create": "/resources",
                    "scan": f"/resources/{encoded}/scans",
                    "cleanup": "/index/cleanup",
                    "history.prune": "/history/prune",
                    "resource.delete": f"/resources/{encoded}",
                    "resource.update": f"/resources/{encoded}",
                }
                path = paths.get(kind, f"/resources/{encoded}/{kind.removeprefix('resource.')}")
                if kind.startswith("profile."):
                    path = f"/resources/{encoded}/profiles/{quote(body['name'], safe='')}"
                method = (
                    "DELETE"
                    if kind in {"resource.delete", "profile.delete"}
                    else (
                        "PUT" if kind == "profile.save" else "PATCH" if kind == "resource.update" else "POST"
                    )
                )
                if ref is not None and "/" in ref:
                    response = await client.post(
                        f"{url}/api/v1/commands",
                        headers={"Idempotency-Key": key},
                        json={"kind": kind, "payload": body, "ref": ref},
                    )
                else:
                    response = await client.request(
                        method,
                        f"{url}/api/v1{path}",
                        headers={"Idempotency-Key": key},
                        json=(body.get("profile") if kind == "profile.save" else body)
                        if method != "DELETE"
                        else None,
                    )
                operation = _decode_operation(response)
                while operation["state"] not in TERMINAL:
                    await asyncio.sleep(0.2)
                    operation = _decode_operation(
                        await client.get(f"{url}/api/v1/operations/{operation['id']}")
                    )
                return _check_result(operation)
            except httpx.TransportError as exc:
                raise _disconnected(identifier, key) from exc
            except RuntimeFailure as exc:
                if exc.status >= 500:
                    raise _disconnected(identifier, key) from exc
                raise
        host, provider = build_cli_host(settings)
        try:
            await host.start_async()
            runtime = await resolve_runtime(provider)
            if secret is not None:
                body["identity_ref"] = await save_credential(secret, body.get("identity_ref"))
            identifier = await _submit_local(runtime, kind, body, ref, key)
            while True:
                local_operation = await runtime.operations.get(identifier)
                if local_operation.state in TERMINAL:
                    return _check_result(operation_view(local_operation))
                if host.lifetime.stopping.is_set():
                    raise _disconnected(identifier, key)
                await asyncio.sleep(0.05)
        finally:
            host.lifetime.stop_application()
            await host.stop_async()


async def manage_operation(
    settings: AppSettings, identifier: str, *, wait: bool = False, retry: bool = False
) -> dict[str, Any]:
    from shovel.db.model.runtime import Operation
    from shovel.runtime.api import operation_view
    from shovel.runtime.errors import NotFound

    async with httpx.AsyncClient(timeout=30) as client:
        url = await _remote(settings, client)
        if url is not None:
            try:
                data = _decode_operation(
                    await (
                        client.post(f"{url}/api/v1/operations/{quote(identifier, safe='')}/retry")
                        if retry
                        else client.get(f"{url}/api/v1/operations/{quote(identifier, safe='')}")
                    )
                )
                while wait and data["state"] not in TERMINAL:
                    await asyncio.sleep(0.2)
                    data = _decode_operation(await client.get(f"{url}/api/v1/operations/{identifier}"))
                return data
            except httpx.TransportError as exc:
                raise _disconnected(identifier) from exc
            except RuntimeFailure as exc:
                if exc.status >= 500:
                    raise _disconnected(identifier) from exc
                raise
    if not wait and not retry:
        context = create_async_database_engine(settings.database.url)
        engine = await anext(context)
        try:
            async with create_async_session_factory(engine)() as session:
                row = await session.get(Operation, identifier)
                if row is None:
                    raise NotFound("操作", identifier)
                return operation_view(row)
        finally:
            await context.aclose()
    host, provider = build_cli_host(settings)
    try:
        await host.start_async()
        runtime = await resolve_runtime(provider)
        if retry:
            await runtime.operations.retry(identifier)
        while True:
            row = await runtime.operations.get(identifier)
            if not wait or row.state in TERMINAL:
                return operation_view(row)
            if host.lifetime.stopping.is_set():
                raise _disconnected(identifier)
            await asyncio.sleep(0.05)
    finally:
        host.lifetime.stop_application()
        await host.stop_async()
