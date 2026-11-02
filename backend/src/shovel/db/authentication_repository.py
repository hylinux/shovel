import hashlib
import json
import time
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import CapabilityScope
from shovel.db.execution_repository import SQLiteExecutionRepository
from shovel.db.identity_repository import SQLiteIdentityRepository
from shovel.db.interaction_repository import project
from shovel.db.model.enterprise import EnterpriseConnectionProfile
from shovel.db.model.identity import AuthenticationRequest as RequestRow
from shovel.db.model.interaction import Interaction
from shovel.db.unit_of_work import sqlite_write
from shovel.execution.domain import ExecutionState
from shovel.identity.domain import AuthenticationRequest, AuthenticationState, TokenRequest
from shovel.runtime.errors import NotFound, RuntimeFailure


def _view(row: RequestRow) -> AuthenticationRequest:
    return AuthenticationRequest(
        id=row.id,
        scope=CapabilityScope(workspace_id=row.workspace_id, user_id=row.user_id),
        request=TokenRequest.model_validate(row.request),
        binding_digest=row.binding_digest,
        reason=AuthenticationState(row.reason),
        state=row.state,
        requires_fresh_challenge=row.requires_fresh_challenge,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


class SQLiteAuthenticationRepository:
    """ES-4 durable pending facts/projections for already-suspended roots; ES-8 owns suspend/resume."""

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
        digest = hashlib.sha256(
            json.dumps(
                [safe.model_dump(mode="json", exclude={"request_id"}), reason.value, fresh],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with sqlite_write(self.sessions) as session:
            execution = await self.executions._find(session, scope, request.execution_id)
            if (
                execution.state != ExecutionState.SUSPENDED.value
                or execution.execution_deadline is None
                or execution.execution_deadline <= self.clock()
                or execution.correlation_id != request.correlation_id
            ):
                raise RuntimeFailure(
                    "Authentication wait requires a bounded suspended execution.", status=409
                )
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
            previous = await session.scalar(
                select(RequestRow).where(
                    RequestRow.execution_id == execution.id,
                    RequestRow.binding_digest == digest,
                )
            )
            if previous is not None:
                return _view(previous)
            if await session.get(RequestRow, request.request_id) is not None:
                raise RuntimeFailure("Authentication request identity conflict.", status=409)
            now = self.clock()
            row = RequestRow(
                id=request.request_id,
                execution_id=execution.id,
                identity_profile_id=identity.id,
                connection_profile_id=connection.id,
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
            await session.commit()
            return result

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
            return _view(await self._find(session, scope, identifier))

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
            return [_view(row) for row in rows]

    async def cancel(
        self,
        scope: CapabilityScope,
        identifier: str,
        *,
        execution_id: str,
    ) -> AuthenticationRequest:
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, scope, identifier)
            if row.execution_id != execution_id or row.state != "pending":
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
