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
from shovel.runtime.schemas import ResourceInput, ScanInput
from shovel.runtime.service import Runtime
from shovel.services.credentials import CredentialRef, build_credential_store
from shovel.services.resource_service import ResourceService


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
    if status.get("service") != "shovel" or status.get("workspace_id") != workspace_id(settings):
        return None
    return url


def _decode(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeFailure("Host 返回了无效的 JSON 响应。") from exc
    if response.is_error:
        error = data.get("error") or {}
        raise RuntimeFailure(
            error.get("message") or f"Host 返回 HTTP {response.status_code}。",
            hint=error.get("hint"),
            status=response.status_code,
        )
    if not isinstance(data, dict):
        raise RuntimeFailure("Host 返回了不支持的操作结果。")
    return data


async def _submit_local(
    runtime: Runtime,
    kind: str,
    payload: dict[str, Any],
    ref: str | None,
) -> str:
    if kind == "resource.create":
        operation = await runtime.operations.create_resource(ResourceInput.model_validate(payload))
    elif kind == "scan":
        operation = await runtime.operations.scan(ref or "", ScanInput.model_validate(payload))
    elif kind == "cleanup":
        operation = await runtime.operations.enqueue(kind, payload)
    elif kind.startswith("resource."):
        operation = await runtime.operations.resource_action(
            ref or "", kind.removeprefix("resource."), payload
        )
    else:
        raise RuntimeFailure(f"不支持的 CLI 操作: {kind}")
    return operation.id


def _check_result(data: dict[str, Any]) -> dict[str, Any]:
    if data["state"] == "failed":
        message = data.get("error") or data.get("result", {}).get("error_detail") or "后台操作失败。"
        if data.get("result", {}).get("metadata_deleted"):
            message = f"资源元数据已删除,但向量清理未完成:{message}"
        raise RuntimeFailure(message, hint=f"操作 {data['id']};可在 Web 重试或运行 shovel scan cleanup。")
    return data


async def execute_command(
    settings: AppSettings,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    *,
    ref: str | None = None,
    secret: SecretStr | None = None,
) -> dict[str, Any]:
    from shovel.runtime.api import operation_view

    body = dict(payload or {})
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
                if ref is not None and kind != "cleanup":
                    context = create_async_database_engine(settings.database.url)
                    engine = await anext(context)
                    try:
                        ref = (await ResourceService(create_async_session_factory(engine)).resolve(ref)).id
                    finally:
                        await context.aclose()
                encoded = quote(ref or "", safe="")
                paths = {
                    "resource.create": "/resources",
                    "scan": f"/resources/{encoded}/scans",
                    "cleanup": "/index/cleanup",
                    "resource.delete": f"/resources/{encoded}",
                    "resource.update": f"/resources/{encoded}",
                }
                path = paths.get(kind, f"/resources/{encoded}/{kind.removeprefix('resource.')}")
                method = (
                    "DELETE"
                    if kind == "resource.delete"
                    else ("PATCH" if kind == "resource.update" else "POST")
                )
                if method == "DELETE":
                    response = await client.delete(f"{url}/api/v1{path}")
                else:
                    response = await client.request(method, f"{url}/api/v1{path}", json=body)
                operation = _decode(response)
                while operation["state"] not in TERMINAL:
                    await asyncio.sleep(0.2)
                    operation = _decode(await client.get(f"{url}/api/v1/operations/{operation['id']}"))
                return _check_result(operation)
            except httpx.TransportError as exc:
                raise RuntimeFailure(
                    "Host 连接已中断;已提交的操作仍持久保存,请查看任务状态,不要重复提交。"
                ) from exc
        host, provider = build_cli_host(settings)
        try:
            await host.start_async()
            runtime = await resolve_runtime(provider)
            if secret is not None:
                body["identity_ref"] = await save_credential(secret, body.get("identity_ref"))
            identifier = await _submit_local(runtime, kind, body, ref)
            while True:
                local_operation = await runtime.operations.get(identifier)
                if local_operation.state in TERMINAL:
                    return _check_result(operation_view(local_operation))
                if host.lifetime.stopping.is_set():
                    raise RuntimeFailure("Host 已停止;操作仍保留,可重启后查询。")
                await asyncio.sleep(0.05)
        finally:
            host.lifetime.stop_application()
            await host.stop_async()
