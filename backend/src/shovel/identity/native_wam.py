"""Installed-Host-only native WAM adapter. Source checkouts have no trust context."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from shovel.capability.domain import CapabilityScope, Permission
from shovel.execution.domain import ExecutionState
from shovel.execution.engine import ExecutionEngine
from shovel.identity.domain import (
    AuthContext,
    AuthenticationMode,
    AuthenticationResolution,
    AuthenticationState,
    BrokerType,
    IdentityProfile,
    TokenRequest,
)
from shovel.identity.errors import (
    AuthConfigurationError,
    AuthenticationDeadlineError,
    AuthenticationRequiredError,
    BrokerUnavailableError,
)
from shovel.identity.waits import authentication_execution_lease


class InstalledBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    prototype: TokenRequest
    target: dict[str, Any]


class InstalledPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    root: str
    python: str
    native: str
    bootstrap: str
    module_paths: tuple[str, ...] = Field(alias="modulePaths")
    files: dict[str, str]
    bindings: tuple[InstalledBinding, ...]
    revision: int
    valid_until: int = Field(alias="validUntil")
    digest: str = Field(alias="_digest")


_installed: tuple[InstalledPolicy, Callable[[], object]] | None = None


def configure_installed_host(content: object, reload: Callable[[], object]) -> None:
    global _installed
    if _installed is not None or not sys.flags.isolated or not sys.flags.no_site:
        raise RuntimeError("Installed Host bootstrap is required.")
    _installed = InstalledPolicy.model_validate(content), reload


def installed_host() -> bool:
    return _installed is not None


def _read_exact(fd: int, count: int) -> bytearray:
    result = bytearray()
    while len(result) < count:
        chunk = os.read(fd, count - len(result))
        if not chunk:
            result[:] = b"\0" * len(result)
            raise ValueError("Native secret channel ended.")
        result.extend(chunk)
    return result


def _read_secret(fd: int, nonce: str, fingerprint: str) -> SecretStr | None:
    data = bytearray()
    try:
        first = os.read(fd, 64)
        if not first:
            return None
        header = bytearray(first)
        header.extend(_read_exact(fd, 64 - len(header)))
        if header != bytes.fromhex(nonce + fingerprint):
            raise ValueError("Native secret binding mismatch.")
        size = struct.unpack(">I", _read_exact(fd, 4))[0]
        if not 1 <= size <= 65536:
            raise ValueError("Native credential size invalid.")
        data = _read_exact(fd, size)
        return SecretStr(data.decode("utf-8"))
    finally:
        data[:] = b"\0" * len(data)
        os.close(fd)


def _frame(fd: int, value: dict[str, Any]) -> None:
    data = json.dumps(value, separators=(",", ":")).encode()
    if len(data) > 32768:
        raise ValueError("Native control message too large.")
    pending = memoryview(struct.pack(">I", len(data)) + data)
    while pending:
        count = os.write(fd, pending)
        if not count:
            raise ValueError("Native control channel ended.")
        pending = pending[count:]


async def _bounded_output(reader: asyncio.StreamReader | None) -> bytes:
    if reader is None:
        raise ValueError("Native output unavailable.")
    result = bytearray()
    while chunk := await reader.read(4097 - len(result)):
        result.extend(chunk)
        if len(result) > 4096:
            raise ValueError("Native output limit exceeded.")
    return bytes(result)


class NativeWamBroker:
    def __init__(self) -> None:
        self._leases: dict[str, tuple[TokenRequest, float, str, Callable[[], Awaitable[None]]]] = {}
        self._active: dict[str, asyncio.Task[AuthContext]] = {}
        self._profiles: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._disconnected: set[str] = set()

    def policy(self) -> InstalledPolicy:
        if _installed is None or os.name != "nt":
            raise BrokerUnavailableError(correlation_id="wam")
        original, reload = _installed
        current = InstalledPolicy.model_validate(reload())
        if current.digest != original.digest:
            raise BrokerUnavailableError(correlation_id="wam")
        return current

    def admit(
        self, resolution: AuthenticationResolution, digest: str, check_active: Callable[[], Awaitable[None]]
    ) -> None:
        request = resolution.approved_request
        if request.identity.identity_profile_id in self._disconnected:
            raise BrokerUnavailableError(correlation_id=request.correlation_id)
        admission = request, resolution.deadline_at, digest, check_active
        self._leases[request.request_id] = admission

        def expire() -> None:
            if self._leases.get(request.request_id) is admission:
                self._leases.pop(request.request_id)

        asyncio.get_running_loop().call_later(max(0, resolution.deadline_at - time.time()), expire)

    async def availability(self, profile: IdentityProfile) -> AuthenticationState:
        # Installation metadata is not proof of a usable silent credential.
        return AuthenticationState.UNAVAILABLE

    async def acquire(self, request: TokenRequest) -> AuthContext:
        request = TokenRequest.model_validate(request)
        async with self._lock:
            lease = self._leases.pop(request.request_id, None)
            if lease is None or lease[0] != request or lease[1] <= time.time():
                raise AuthConfigurationError(correlation_id=request.correlation_id)
            policy = await asyncio.to_thread(self.policy)
            if policy.digest != lease[2]:
                raise AuthConfigurationError(correlation_id=request.correlation_id)
            await lease[3]()
            task = asyncio.create_task(self._acquire(request, lease[1], policy))
            self._active[request.request_id] = task
            self._profiles[request.request_id] = request.identity.identity_profile_id
            try:
                try:
                    async with asyncio.timeout(max(0, lease[1] - time.time())):
                        return await task
                except TimeoutError:
                    raise AuthenticationDeadlineError(correlation_id=request.correlation_id) from None
            finally:
                self._active.pop(request.request_id, None)
                self._profiles.pop(request.request_id, None)

    async def _acquire(self, request: TokenRequest, deadline: float, policy: InstalledPolicy) -> AuthContext:
        import msvcrt

        raw = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        fingerprint = hashlib.sha256(raw).hexdigest()
        nonce = os.urandom(32).hex()
        control_read = control_write = secret_read = secret_write = -1
        process: asyncio.subprocess.Process | None = None
        secret_task: asyncio.Task[SecretStr | None] | None = None
        result_context: AuthContext | None = None
        try:
            control_read, control_write = os.pipe()
            secret_read, secret_write = os.pipe()
            handles = [msvcrt.get_osfhandle(control_read), msvcrt.get_osfhandle(secret_write)]
            for handle in handles:
                os.set_handle_inheritable(handle, True)
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": handles}
            environment = {
                key: value
                for key, value in os.environ.items()
                if key.upper()
                in {"SYSTEMROOT", "WINDIR", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP"}
            }
            process = await asyncio.create_subprocess_exec(
                str(Path(policy.root) / policy.native),
                "bridge",
                *(str(handle) for handle in handles),
                startupinfo=startup,
                close_fds=True,
                env=environment,
                cwd=policy.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            for handle in handles:
                os.set_handle_inheritable(handle, False)
            os.close(control_read)
            control_read = -1
            os.close(secret_write)
            secret_write = -1
            secret_task = asyncio.create_task(
                asyncio.to_thread(_read_secret, secret_read, nonce, fingerprint)
            )
            secret_read = -1
            await asyncio.to_thread(
                _frame,
                control_write,
                {
                    "version": 1,
                    "nonce": nonce,
                    "deadline": deadline,
                    "request": base64.b64encode(raw).decode("ascii"),
                },
            )
            public, diagnostics = await asyncio.gather(
                _bounded_output(process.stdout), _bounded_output(process.stderr)
            )
            code = await process.wait()
            # stderr is never forwarded; only allow our enum-only diagnostic envelopes.
            for line in diagnostics.splitlines():
                item = json.loads(line)
                if set(item) != {"diagnostic"} or item["diagnostic"] not in {
                    "native_cancel_failed",
                    "native_close_failed",
                    "native_status_failed",
                    "late_native_failure",
                    "control_channel_closed_or_invalid",
                }:
                    raise ValueError("Unexpected native diagnostics.")
            receipt = json.loads(public)
            current_policy = await asyncio.to_thread(self.policy)
            if (
                set(receipt)
                != {
                    "version",
                    "nonce",
                    "fingerprint",
                    "expires_at",
                    "status",
                    "native_operation_canceled",
                    "scope",
                    "production_ready",
                    "live_acceptance_complete",
                }
                or type(receipt.get("version")) is not int
                or receipt.get("version") != 1
                or type(receipt.get("native_operation_canceled")) is not bool
                or receipt.get("scope") != "wam_host_bridge"
                or receipt.get("nonce") != nonce
                or receipt.get("fingerprint") != fingerprint
                or receipt.get("expires_at") != deadline
                or receipt.get("production_ready") is not False
                or receipt.get("live_acceptance_complete") is not False
                or time.time() >= deadline
                or current_policy.digest != policy.digest
            ):
                raise ValueError("Native authentication result rejected.")
            if code == 3 and receipt.get("status") == "interaction_required":
                raise AuthenticationRequiredError(correlation_id=request.correlation_id)
            if code != 0 or receipt.get("status") != "success":
                raise ValueError("Native authentication did not succeed.")
            credential = await secret_task
            if credential is None or time.time() >= deadline:
                raise ValueError("Native credential unavailable.")
            result_context = AuthContext(request, credential, expires_at=deadline)
            return result_context
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                try:
                    await asyncio.wait_for(asyncio.to_thread(_frame, control_write, {"cancel": nonce}), 0.5)
                except OSError:
                    structlog.get_logger("shovel.identity").warning(
                        "authentication.cancel_channel_closed", request_id=request.request_id
                    )
                except TimeoutError:
                    structlog.get_logger("shovel.identity").warning(
                        "authentication.cancel_delivery_unconfirmed", request_id=request.request_id
                    )
            raise
        except AuthenticationRequiredError:
            raise
        except Exception:
            raise BrokerUnavailableError(correlation_id=request.correlation_id) from None
        finally:

            async def finish() -> None:
                try:
                    if process is not None:
                        try:
                            await asyncio.wait_for(asyncio.shield(process.wait()), 2.5)
                        except TimeoutError:
                            process.kill()
                            await process.wait()
                finally:
                    for fd in (control_read, control_write, secret_read, secret_write):
                        if fd >= 0:
                            os.close(fd)
                    if secret_task is not None:
                        await asyncio.gather(secret_task, return_exceptions=True)

            cleanup = asyncio.create_task(finish())
            interrupted = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            cleanup.result()
            if interrupted:
                if result_context is not None:
                    result_context.close()
                raise asyncio.CancelledError

    async def cancel(self, request_id: str) -> None:
        task = self._active.get(request_id)
        if task is None:
            raise BrokerUnavailableError(correlation_id=request_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def disconnect(self, profile: IdentityProfile) -> None:
        self._disconnected.add(profile.id)
        for identifier, lease in tuple(self._leases.items()):
            if lease[0].identity.identity_profile_id == profile.id:
                self._leases.pop(identifier)
        tasks = [
            task for identifier, task in self._active.items() if self._profiles[identifier] == profile.id
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        self._leases.clear()
        tasks = tuple(self._active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class NativeWamResolver:
    def __init__(self, engine: ExecutionEngine, broker: NativeWamBroker) -> None:
        self.engine, self.broker = engine, broker

    async def resolve(self, request: TokenRequest) -> AuthenticationResolution:
        request = TokenRequest.model_validate(request)
        policy = await asyncio.to_thread(self.broker.policy)
        lease = authentication_execution_lease.get()
        if lease is None or lease.execution.id != request.execution_id:
            raise AuthConfigurationError(correlation_id=request.correlation_id)
        matches = []
        for item in policy.bindings:
            prototype = item.prototype
            prototype = prototype.model_copy(
                update={
                    name: getattr(request, name) for name in ("request_id", "execution_id", "correlation_id")
                }
            )
            if prototype == request:
                matches.append(item)
        if (
            len(matches) != 1
            or request.interaction_mode != "silent"
            or request.force_refresh
            or request.claims_challenge_ref
        ):
            raise AuthConfigurationError(correlation_id=request.correlation_id)
        scope = CapabilityScope(workspace_id=request.identity.workspace_id, user_id=request.identity.user_id)
        execution = await self.engine.repository.get(scope, request.execution_id)
        permission = Permission(action="enterprise.read", resource=request.connection_profile_id)
        content = execution.snapshot.capability.content
        deadline = min(execution.deadline_at or 0, policy.valid_until)
        if (
            execution.state != ExecutionState.RUNNING
            or execution.correlation_id != request.correlation_id
            or execution.snapshot.context.scope != scope
            or permission not in execution.snapshot.granted_permissions
            or permission not in content.required_permissions
            or deadline <= time.time()
            or deadline - time.time() > 300
            or len(content.connection_requirements) != 1
            or content.connection_requirements[0].connection_ref != request.connection_profile_id
        ):
            raise AuthConfigurationError(correlation_id=request.correlation_id)

        async def check_active() -> None:
            await self.engine.repository.check_active(lease, self.engine.invocations.security)

        await check_active()
        profile = IdentityProfile(
            id=request.identity.identity_profile_id,
            display_name="Installed identity",
            tenant_binding=request.identity.tenant_id,
            revision=request.identity.identity_revision,
            broker_type=BrokerType.NATIVE_WAM,
            authentication_mode=AuthenticationMode.WAM,
            enabled=True,
            created_at=0,
            updated_at=0,
        )
        resolution = AuthenticationResolution(approved_request=request, profile=profile, deadline_at=deadline)
        self.broker.admit(resolution, policy.digest, check_active)
        return resolution
