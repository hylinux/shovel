from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shovel.db.model.enterprise_execution import AuthenticationCompletion
from shovel.db.model.identity import AuthenticationRequest
from shovel.db.model.interaction import Interaction
from shovel.db.model.runtime import Operation


async def retire_authentication(session: AsyncSession, execution: Operation, now: float) -> None:
    rows = await session.scalars(
        select(AuthenticationRequest).where(
            AuthenticationRequest.execution_id == execution.id,
            AuthenticationRequest.state == "pending",
            AuthenticationRequest.id.not_in(select(AuthenticationCompletion.request_id)),
        )
    )
    for request in rows:
        request.state = "expired" if request.expires_at <= now else "cancelled"
        projection = await session.scalar(
            select(Interaction).where(
                Interaction.source_kind == "authentication_request", Interaction.source_id == request.id
            )
        )
        if projection is not None:
            projection.state, projection.actions = "resolved", ["VIEW"]
