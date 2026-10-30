from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import CapabilityScope
from shovel.db.execution_repository import _record
from shovel.db.model.interaction import InputRequest, Interaction, Notification, NotificationDelivery
from shovel.db.model.runtime import Operation
from shovel.db.model.security import ApprovalRequest
from shovel.db.unit_of_work import sqlite_write
from shovel.domain.identity import new_id
from shovel.interaction.domain import Interaction as InteractionView
from shovel.interaction.domain import NotificationDelivery as DeliveryView
from shovel.runtime.errors import NotFound, RuntimeFailure


async def project(
    session: AsyncSession,
    execution: Operation,
    source_kind: str,
    source_id: str,
    kind: str,
    message: str,
    actions: list[str],
    now: float,
    *,
    identifier: str | None = None,
    notification: Notification | None = None,
) -> Interaction:
    row = await session.scalar(
        select(Interaction).where(
            Interaction.source_kind == source_kind,
            Interaction.source_id == source_id,
        )
    )
    if row is not None:
        return row
    scope = _record(execution).snapshot.context.scope
    row = Interaction(
        id=identifier or new_id("interaction"),
        execution_id=execution.id,
        workspace_id=scope.workspace_id,
        user_id=scope.user_id,
        source_kind=source_kind,
        source_id=source_id,
        kind=kind,
        message=message,
        actions=actions,
        state="open",
        created_at=now,
    )
    session.add(row)
    await session.flush()
    await notice(session, row, now, notification=notification)
    return row


async def notice(
    session: AsyncSession,
    interaction: Interaction,
    now: float,
    *,
    notification: Notification | None = None,
    suffix: str = "",
) -> None:
    if notification is None:
        node_id = f"interaction.{interaction.id}{suffix}"
        notification = await session.scalar(
            select(Notification).where(
                Notification.execution_id == interaction.execution_id,
                Notification.node_id == node_id,
            )
        )
        if notification is None:
            notification = Notification(
                id=new_id("notification"),
                execution_id=interaction.execution_id,
                workspace_id=interaction.workspace_id,
                user_id=interaction.user_id,
                node_id=node_id,
                message=interaction.message,
                payload={},
            )
            session.add(notification)
            await session.flush()
    for channel in ("in_app", "console"):
        previous = await session.scalar(
            select(NotificationDelivery.id).where(
                NotificationDelivery.notification_id == notification.id,
                NotificationDelivery.channel == channel,
            )
        )
        if previous is None:
            session.add(
                NotificationDelivery(
                    notification_id=notification.id,
                    interaction_id=interaction.id,
                    channel=channel,
                    state="delivered" if channel == "in_app" else "pending",
                    delivered_at=now if channel == "in_app" else None,
                    next_at=now,
                )
            )


def _view(row: Interaction) -> InteractionView:
    return InteractionView.model_validate(
        {field: getattr(row, field) for field in InteractionView.model_fields}
    )


class SQLiteInteractionRepository:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, clock: Callable[[], float] = time.time
    ) -> None:
        self.sessions, self.clock = sessions, clock

    async def refresh(self) -> int:
        async with sqlite_write(self.sessions) as session:
            now = self.clock()
            approvals = list(
                await session.scalars(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.id.not_in(
                            select(Interaction.source_id).where(Interaction.source_kind == "approval")
                        ),
                    )
                    .limit(100)
                )
            )
            for request in approvals:
                execution = await session.get(Operation, request.execution_id)
                assert execution is not None
                await project(
                    session,
                    execution,
                    "approval",
                    request.id,
                    "approval",
                    "Approval required.",
                    ["VIEW", "APPROVE", "REJECT", "DISMISS", "SNOOZE"],
                    now,
                )
            inputs = list(
                await session.scalars(
                    select(InputRequest)
                    .where(
                        InputRequest.id.not_in(
                            select(Interaction.source_id).where(Interaction.source_kind == "input_request")
                        ),
                    )
                    .limit(100)
                )
            )
            for request_input in inputs:
                execution = await session.get(Operation, request_input.execution_id)
                assert execution is not None
                await project(
                    session,
                    execution,
                    "input_request",
                    request_input.id,
                    "input_request",
                    request_input.prompt,
                    ["VIEW", "PROVIDE_INPUT", "DISMISS", "SNOOZE"],
                    now,
                    identifier=request_input.id,
                )
            notifications = list(
                await session.scalars(
                    select(Notification)
                    .where(
                        Notification.id.not_in(select(NotificationDelivery.notification_id)),
                    )
                    .limit(100)
                )
            )
            for notification in notifications:
                execution = await session.get(Operation, notification.execution_id)
                assert execution is not None
                await project(
                    session,
                    execution,
                    "notification",
                    notification.id,
                    "information",
                    notification.message,
                    ["VIEW", "DISMISS"],
                    now,
                    notification=notification,
                )
            executions = list(
                await session.scalars(
                    select(Operation)
                    .where(
                        Operation.execution_data.is_not(None),
                        or_(
                            and_(
                                Operation.state.in_(("succeeded", "failed", "cancelled")),
                                Operation.id.not_in(
                                    select(Interaction.source_id).where(Interaction.source_kind == "result")
                                ),
                            ),
                            and_(
                                Operation.state == "suspended",
                                func.json_extract(Operation.execution_control, "$.wait_reason")
                                == "interrupted",
                                Operation.id.not_in(
                                    select(Interaction.source_id).where(
                                        Interaction.source_kind == "interrupted"
                                    )
                                ),
                            ),
                        ),
                    )
                    .limit(100)
                )
            )
            created = len(approvals) + len(inputs) + len(notifications)
            for execution in executions:
                if execution.state in {"succeeded", "failed", "cancelled"}:
                    await project(
                        session,
                        execution,
                        "result",
                        execution.id,
                        "result",
                        f"Execution {execution.state}.",
                        ["VIEW", "DISMISS"],
                        now,
                    )
                elif execution.execution_control.get("wait_reason") == "interrupted":
                    await project(
                        session,
                        execution,
                        "interrupted",
                        execution.id,
                        "warning",
                        "Execution interrupted; review before resuming.",
                        ["VIEW", "DISMISS", "CONFIGURE"],
                        now,
                    )
            open_rows = list(
                await session.scalars(
                    select(Interaction)
                    .join(Operation, Interaction.execution_id == Operation.id)
                    .where(
                        Interaction.state.in_(("open", "dismissed")),
                        or_(
                            and_(
                                Interaction.source_kind.in_(("approval", "input_request")),
                                Operation.state.in_(("succeeded", "failed", "cancelled")),
                            ),
                            and_(
                                Interaction.source_kind == "approval",
                                Interaction.source_id.in_(
                                    select(ApprovalRequest.id).where(ApprovalRequest.state != "pending")
                                ),
                            ),
                            and_(
                                Interaction.source_kind == "input_request",
                                Interaction.source_id.in_(
                                    select(InputRequest.id).where(InputRequest.state != "pending")
                                ),
                            ),
                        ),
                    )
                    .limit(100)
                )
            )
            for row in open_rows:
                execution = await session.get(Operation, row.execution_id)
                pending = (
                    (
                        (approval := await session.get(ApprovalRequest, row.source_id)) is not None
                        and approval.state == "pending"
                    )
                    if row.source_kind == "approval"
                    else (
                        (input_fact := await session.get(InputRequest, row.source_id)) is not None
                        and input_fact.state == "pending"
                    )
                )
                if (
                    not pending
                    or execution is None
                    or execution.state in {"succeeded", "failed", "cancelled"}
                ):
                    row.state, row.actions, row.snoozed_until = "resolved", ["VIEW"], None
            missing_deliveries = list(
                await session.scalars(
                    select(Interaction)
                    .where(
                        Interaction.id.not_in(select(NotificationDelivery.interaction_id)),
                    )
                    .limit(100)
                )
            )
            for row in missing_deliveries:
                original = (
                    await session.get(Notification, row.source_id)
                    if row.source_kind == "notification"
                    else None
                )
                await notice(session, row, now, notification=original)
            await session.commit()
            return created

    async def _find(self, session: AsyncSession, scope: CapabilityScope, identifier: str) -> Interaction:
        row = await session.get(Interaction, identifier)
        if row is None or row.workspace_id != scope.workspace_id or row.user_id != scope.user_id:
            raise NotFound("Interaction", identifier)
        return row

    async def get(self, scope: CapabilityScope, identifier: str) -> InteractionView:
        async with self.sessions() as session:
            return _view(await self._find(session, scope, identifier))

    async def list(self, scope: CapabilityScope) -> list[InteractionView]:
        async with self.sessions() as session:
            return [
                _view(row)
                for row in await session.scalars(
                    select(Interaction)
                    .where(
                        Interaction.workspace_id == scope.workspace_id,
                        Interaction.user_id == scope.user_id,
                    )
                    .order_by(Interaction.created_at.desc(), Interaction.id)
                    .limit(100)
                )
            ]

    async def dismiss(self, scope: CapabilityScope, identifier: str) -> InteractionView:
        async with sqlite_write(self.sessions) as session:
            row = await self._find(session, scope, identifier)
            if row.state == "open":
                row.state = "dismissed"
            await session.commit()
            return _view(row)

    async def claim_delivery(self) -> DeliveryView | None:
        async with sqlite_write(self.sessions) as session:
            row = await session.scalar(
                select(NotificationDelivery)
                .join(Interaction)
                .where(
                    NotificationDelivery.channel == "console",
                    NotificationDelivery.state == "pending",
                    NotificationDelivery.next_at <= self.clock(),
                    NotificationDelivery.attempts < 3,
                    Interaction.state == "open",
                    (Interaction.snoozed_until.is_(None)) | (Interaction.snoozed_until <= self.clock()),
                )
                .order_by(NotificationDelivery.next_at, NotificationDelivery.id)
                .limit(1)
            )
            if row is None:
                return None
            notification = await session.get(Notification, row.notification_id)
            assert notification is not None
            row.state, row.token = "delivering", new_id("delivery_claim")
            row.attempts += 1
            await session.commit()
            return DeliveryView(
                id=row.id,
                notification_id=row.notification_id,
                interaction_id=row.interaction_id,
                channel="console",
                message=notification.message,
                token=row.token,
            )

    async def finish_delivery(self, delivery: DeliveryView, *, failed: bool = False) -> None:
        async with sqlite_write(self.sessions) as session:
            row = await session.get(NotificationDelivery, delivery.id)
            if row is None or row.state != "delivering" or row.token != delivery.token:
                raise RuntimeFailure("Stale notification delivery claim.", status=409)
            row.state = ("failed" if row.attempts >= 3 else "pending") if failed else "delivered"
            row.error = "notification_delivery_failed" if failed else None
            row.delivered_at = None if failed else self.clock()
            row.next_at = self.clock() + min(2**row.attempts, 60)
            row.token = None
            await session.commit()
