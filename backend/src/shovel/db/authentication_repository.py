import hashlib
import json
import time
from collections.abc import Callable

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import CapabilityScope, Digest
from shovel.db.authentication_lifecycle import retire_authentication
from shovel.db.execution_repository import SQLiteExecutionRepository, _record
from shovel.db.identity_repository import SQLiteIdentityRepository
from shovel.db.interaction_repository import project
from shovel.db.model.enterprise import EnterpriseConnectionProfile
from shovel.db.model.enterprise_execution import AuthenticationCompletion
from shovel.db.model.identity import AuthenticationRequest as RequestRow
from shovel.db.model.interaction import Interaction
from shovel.db.unit_of_work import sqlite_write
from shovel.execution.domain import BudgetUsage, Execution, ExecutionLease, ExecutionState
from shovel.identity.domain import AuthContext, AuthenticationRequest, AuthenticationState, TokenRequest
from shovel.identity.waits import AuthenticationWaitRequired
from shovel.runtime.errors import NotFound, RuntimeFailure


def _view(row: RequestRow, *, completed: bool = False) -> AuthenticationRequest:
    return AuthenticationRequest(
        id=row.id,
        scope=CapabilityScope(workspace_id=row.workspace_id, user_id=row.user_id),
        request=TokenRequest.model_validate(row.request),
        binding_digest=row.binding_digest,
        reason=AuthenticationState(row.reason),
        state="satisfied" if completed else row.state,
        requires_fresh_challenge=row.requires_fresh_challenge,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


class SQLiteAuthenticationRepository:
    """Atomic Engine waits and immutable silent-authentication completion facts."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.sessions, self.clock = sessions, clock
        self.executions = SQLiteExecutionRepository(sessions, clock=clock)
        self.identities = SQLiteIdentityRepository(sessions, clock=clock)

    async def create(
        self,
        scope: CapabilityScope,
        request: TokenRequest,
        reason: AuthenticationState,
    ) -> AuthenticationRequest:
        async with sqlite_write(self.sessions) as session:
            result = await self._create(session, scope, request, reason)
            await session.commit()
            return result

    async def _profiles(self, session: AsyncSession, scope: CapabilityScope, request: TokenRequest) -> None:
        if request.identity.workspace_id != scope.workspace_id or request.identity.user_id != scope.user_id:
            raise RuntimeFailure("Authentication profile scope mismatch.", status=409)
        identity = await self.identities._find(session, scope, request.identity.identity_profile_id)
        connection = await session.scalar(
            select(EnterpriseConnectionProfile).where(
                EnterpriseConnectionProfile.id == request.connection_profile_id,
                EnterpriseConnectionProfile.workspace_id == scope.workspace_id,
                EnterpriseConnectionProfile.user_id == scope.user_id,
            )
        )
        if (
            identity.revision != request.identity.identity_revision
            or identity.configuration["tenant_binding"] != request.identity.tenant_id
            or connection is None
            or connection.revision != request.connection_revision
            or connection.manifest_digest != request.manifest_digest
            or connection.manifest_version != request.provider_version
            or connection.configuration["provider_id"] != request.provider_id
            or connection.configuration["identity_profile_ref"] != identity.id
            or connection.configuration["tenant_binding"] != request.identity.tenant_id
        ):
            raise RuntimeFailure("Authentication wait profile binding has changed.", status=409)

    async def _create(
        self,
        session: AsyncSession,
        scope: CapabilityScope,
        request: TokenRequest,
        reason: AuthenticationState,
        *,
        attempt: int | None = None,
    ) -> AuthenticationRequest:
        request = TokenRequest.model_validate(request)
        if request.identity.workspace_id != scope.workspace_id or request.identity.user_id != scope.user_id:
            raise RuntimeFailure("Authentication wait scope mismatch.", status=409)
        fresh = (
            request.claims_challenge_ref is not None
            or reason == AuthenticationState.CLAIMS_CHALLENGE_REQUIRED
        )
        safe = TokenRequest.model_validate(
            {
                **request.model_dump(),
                "claims_challenge_ref": None,
                "interaction_mode": "silent",
                "force_refresh": False,
            }
        )
        fingerprint: list[object] = [
            safe.model_dump(mode="json", exclude={"request_id"}),
            reason.value,
            fresh,
        ]
        if attempt is not None:
            fingerprint.append(attempt)
        digest = hashlib.sha256(
            json.dumps(
                fingerprint,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        execution = await self.executions._find(session, scope, request.execution_id)
        if (
            execution.state != ExecutionState.SUSPENDED.value
            or execution.execution_deadline is None
            or execution.execution_deadline <= self.clock()
            or execution.correlation_id != request.correlation_id
        ):
            raise RuntimeFailure("Authentication wait requires a bounded suspended execution.", status=409)
        await self._profiles(session, scope, request)
        previous = await session.scalar(
            select(RequestRow).where(
                RequestRow.execution_id == execution.id, RequestRow.binding_digest == digest
            )
        )
        if previous is not None:
            return _view(
                previous, completed=await session.get(AuthenticationCompletion, previous.id) is not None
            )
        if await session.get(RequestRow, request.request_id) is not None:
            raise RuntimeFailure("Authentication request identity conflict.", status=409)
        now = self.clock()
        row = RequestRow(
            id=request.request_id,
            execution_id=execution.id,
            identity_profile_id=request.identity.identity_profile_id,
            connection_profile_id=request.connection_profile_id,
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
            request=safe.model_dump(mode="json"),
            binding_digest=digest,
            reason=reason.value,
            state="pending",
            requires_fresh_challenge=fresh,
            created_at=now,
            expires_at=execution.execution_deadline,
        )
        result = _view(row)
        session.add(row)
        await session.flush()
        await project(
            session,
            execution,
            "authentication_request",
            row.id,
            "warning",
            "Enterprise authentication requires attention; no business request has been resumed.",
            ["VIEW", "CONFIGURE", "DISMISS"],
            now,
        )
        return result

    async def suspend(self, lease: ExecutionLease, wait: AuthenticationWaitRequired) -> Execution:
        digest = TypeAdapter(Digest).validate_python(wait.operation_digest)
        scope = lease.execution.snapshot.context.scope
        async with sqlite_write(self.sessions) as session:
            execution = await self.executions._find(session, scope, lease.execution.id)
            self.executions._leased(execution, lease)
            previous_digest = execution.execution_control.get("enterprise_operation_digest")
            if previous_digest is not None and previous_digest != digest:
                raise RuntimeFailure("Frozen enterprise target changed.", status=409)
            if wait.request.execution_id != execution.id or await self.executions._children(
                session, execution
            ):
                raise RuntimeFailure("Authentication wait requires its exact leaf execution.", status=409)
            await self.executions._charge(session, execution, BudgetUsage())
            await retire_authentication(session, execution, self.clock())
            await self.executions._end_attempt(
                session, execution, ExecutionState.SUSPENDED, reason="authentication_required"
            )
            execution.state = "suspended"
            execution.row_version += 1
            await session.flush()
            request = await self._create(
                session, scope, wait.request, wait.reason, attempt=execution.attempts
            )
            execution.execution_control = {
                **execution.execution_control,
                "wait_reason": "waiting_for_authentication",
                "enterprise_operation_digest": digest,
                "authentication": {"request_id": request.id, "operation_digest": digest},
            }
            await session.commit()
            return _record(execution)

    async def bind_operation(self, lease: ExecutionLease, operation_digest: Digest) -> None:
        operation_digest = TypeAdapter(Digest).validate_python(operation_digest)
        async with sqlite_write(self.sessions) as session:
            row = await self.executions._find(
                session, lease.execution.snapshot.context.scope, lease.execution.id
            )
            self.executions._leased(row, lease)
            await self.executions._charge(session, row, BudgetUsage())
            previous = row.execution_control.get("enterprise_operation_digest")
            if previous is not None and previous != operation_digest:
                raise RuntimeFailure("Frozen enterprise target changed.", status=409)
            row.execution_control = {**row.execution_control, "enterprise_operation_digest": operation_digest}
            await session.commit()

    async def authenticated(
        self, lease: ExecutionLease, request: TokenRequest, context: AuthContext, operation_digest: Digest
    ) -> None:
        request = TokenRequest.model_validate(request)
        operation_digest = TypeAdapter(Digest).validate_python(operation_digest)
        scope = lease.execution.snapshot.context.scope
        async with sqlite_write(self.sessions) as session:
            execution = await self.executions._find(session, scope, lease.execution.id)
            self.executions._leased(execution, lease)
            if (
                request.execution_id != execution.id
                or request.correlation_id != execution.correlation_id
                or execution.execution_control.get("enterprise_operation_digest") != operation_digest
            ):
                raise RuntimeFailure("Authentication does not bind this execution.", status=409)
            await self.executions._charge(session, execution, BudgetUsage())
            await self._profiles(session, scope, request)
            marker = execution.execution_control.get("authentication")
            if isinstance(marker, dict):
                if marker.get("operation_digest") != operation_digest:
                    raise RuntimeFailure("Frozen enterprise target changed.", status=409)
                identifier = marker.get("request_id")
                if not isinstance(identifier, str):
                    raise RuntimeFailure("Authentication wait binding is invalid.", status=409)
                pending = await self._find(session, scope, identifier)
                approved = TokenRequest.model_validate(pending.request).model_copy(
                    update={
                        "request_id": request.request_id,
                        "claims_challenge_ref": request.claims_challenge_ref,
                    }
                )
                request.validate_ceiling(approved)
                if pending.state != "pending" or pending.expires_at <= self.clock():
                    raise RuntimeFailure("Authentication wait is no longer pending.", status=409)
                if pending.requires_fresh_challenge and request.claims_challenge_ref is None:
                    raise RuntimeFailure(
                        "Authentication requires a newly resolved claims challenge.", status=409
                    )
                context.validate(request, now=self.clock())
                completion = await session.get(AuthenticationCompletion, pending.id)
                if pending.execution_id != execution.id or (
                    completion is not None
                    and (
                        completion.execution_id != execution.id
                        or completion.binding_digest != pending.binding_digest
                        or completion.operation_digest != operation_digest
                    )
                ):
                    raise RuntimeFailure("Authentication completion binding is invalid.", status=409)
                if completion is None:
                    session.add(
                        AuthenticationCompletion(
                            request_id=pending.id,
                            execution_id=execution.id,
                            binding_digest=pending.binding_digest,
                            operation_digest=operation_digest,
                            satisfied_at=self.clock(),
                        )
                    )
                projection = await session.scalar(
                    select(Interaction).where(
                        Interaction.source_kind == "authentication_request",
                        Interaction.source_id == pending.id,
                    )
                )
                if projection is not None:
                    projection.state, projection.actions = "resolved", ["VIEW"]
            context.validate(request, now=self.clock())
            await session.commit()

    async def prepare_output(self, lease: ExecutionLease) -> None:
        async with sqlite_write(self.sessions) as session:
            execution = await self.executions._find(
                session, lease.execution.snapshot.context.scope, lease.execution.id
            )
            self.executions._leased(execution, lease)
            await self.executions._charge(session, execution, BudgetUsage())
            if execution.execution_control.get("enterprise_release_started"):
                raise RuntimeFailure("Enterprise output requires reconciliation before replay.", status=409)
            execution.execution_control = {**execution.execution_control, "enterprise_release_started": True}
            execution.row_version += 1
            await session.commit()

    async def _find(self, session: AsyncSession, scope: CapabilityScope, identifier: str) -> RequestRow:
        row = await session.scalar(
            select(RequestRow).where(
                RequestRow.id == identifier,
                RequestRow.workspace_id == scope.workspace_id,
                RequestRow.user_id == scope.user_id,
            )
        )
        if row is None:
            raise NotFound("Authentication request", identifier)
        return row

    async def get(self, scope: CapabilityScope, identifier: str) -> AuthenticationRequest:
        async with self.sessions() as session:
            return _view(
                await self._find(session, scope, identifier),
                completed=await session.get(AuthenticationCompletion, identifier) is not None,
            )

    async def list(self, scope: CapabilityScope) -> list[AuthenticationRequest]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(RequestRow)
                .where(
                    RequestRow.workspace_id == scope.workspace_id,
                    RequestRow.user_id == scope.user_id,
                )
                .order_by(RequestRow.id)
            )
            return [
                _view(row, completed=await session.get(AuthenticationCompletion, row.id) is not None)
                for row in rows
            ]

    async def cancel(
        self,
        scope: CapabilityScope,
        identifier: str,
        *,
        execution_id: str,
    ) -> AuthenticationRequest:
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, scope, identifier)
            if (
                row.execution_id != execution_id
                or row.state != "pending"
                or await session.get(AuthenticationCompletion, row.id) is not None
            ):
                raise RuntimeFailure("Authentication request is not pending for this execution.", status=409)
            row.state = "expired" if row.expires_at <= self.clock() else "cancelled"
            projection = await session.scalar(
                select(Interaction).where(
                    Interaction.source_kind == "authentication_request",
                    Interaction.source_id == row.id,
                )
            )
            if projection is not None:
                projection.state, projection.actions = "resolved", ["VIEW"]
            result = _view(row)
            await session.commit()
            return result
