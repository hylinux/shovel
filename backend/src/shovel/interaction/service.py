from __future__ import annotations

from typing import Protocol

import structlog
from pydantic import JsonValue
from rich.console import Console
from rich.text import Text

from shovel.capability.domain import CapabilityScope
from shovel.execution.control import ExecutionWakeupService
from shovel.interaction.domain import Interaction, InteractionAction, NotificationDelivery
from shovel.runtime.errors import RuntimeFailure
from shovel.security.services import ApprovalService
from shovel.workflow.service import WorkflowService


class InteractionRepository(Protocol):
    async def refresh(self) -> int: ...
    async def get(self, scope: CapabilityScope, identifier: str) -> Interaction: ...
    async def list(self, scope: CapabilityScope) -> list[Interaction]: ...
    async def dismiss(self, scope: CapabilityScope, identifier: str) -> Interaction: ...
    async def claim_delivery(self) -> NotificationDelivery | None: ...
    async def finish_delivery(self, delivery: NotificationDelivery, *, failed: bool = False) -> None: ...


class NotificationSink(Protocol):
    async def deliver(self, delivery: NotificationDelivery) -> None: ...


class RichNotificationSink:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console(stderr=True)

    async def deliver(self, delivery: NotificationDelivery) -> None:
        self.console.print(Text(f"[{delivery.interaction_id}] {delivery.message}"))


class NotificationService:
    def __init__(self, repository: InteractionRepository, sink: NotificationSink | None = None) -> None:
        self.repository, self.sink = repository, sink or RichNotificationSink()

    async def run_once(self) -> bool:
        delivery = await self.repository.claim_delivery()
        if delivery is None:
            return False
        try:
            await self.sink.deliver(delivery)
        except Exception as exc:
            structlog.get_logger("shovel.notification").error(
                "notification.delivery_failed",
                notification_id=delivery.notification_id,
                error_type=type(exc).__name__,
            )
            await self.repository.finish_delivery(delivery, failed=True)
        else:
            await self.repository.finish_delivery(delivery)
        return True


class InteractionService:
    def __init__(
        self,
        repository: InteractionRepository,
        approvals: ApprovalService,
        inputs: WorkflowService,
        wakeups: ExecutionWakeupService,
    ) -> None:
        self.repository, self.approvals, self.inputs, self.wakeups = repository, approvals, inputs, wakeups

    async def refresh(self) -> int:
        return await self.repository.refresh()

    async def list(self, scope: CapabilityScope) -> list[Interaction]:
        return await self.repository.list(scope)

    async def act(
        self,
        scope: CapabilityScope,
        identifier: str,
        action: InteractionAction,
        *,
        response: JsonValue = None,
        wake_at: float | None = None,
    ) -> Interaction:
        await self.refresh()
        interaction = await self.repository.get(scope, identifier)
        if action not in interaction.actions or (
            interaction.state != "open" and action != InteractionAction.VIEW
        ):
            raise RuntimeFailure("Interaction action is not available.", status=409)
        if action == InteractionAction.APPROVE:
            await self.approvals.approve(
                scope, interaction.execution_id, interaction.source_id, user_id=scope.user_id
            )
        elif action == InteractionAction.REJECT:
            await self.approvals.reject(
                scope, interaction.execution_id, interaction.source_id, user_id=scope.user_id
            )
        elif action == InteractionAction.PROVIDE_INPUT:
            await self.inputs.respond(
                scope,
                interaction.execution_id,
                interaction.source_id,
                user_id=scope.user_id,
                response=response,
            )
        elif action == InteractionAction.DISMISS:
            return await self.repository.dismiss(scope, identifier)
        elif action == InteractionAction.SNOOZE:
            if wake_at is None:
                raise RuntimeFailure("Snooze requires wake_at.")
            await self.wakeups.snooze(
                scope, interaction.execution_id, wake_at=wake_at, interaction_id=identifier
            )
        # VIEW and CONFIGURE expose the projection; configuring is not an implicit mutation.
        await self.refresh()
        return await self.repository.get(scope, identifier)
