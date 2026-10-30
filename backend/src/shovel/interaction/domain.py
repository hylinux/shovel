from enum import StrEnum
from typing import Literal

from shovel.capability.domain import Value


class InteractionAction(StrEnum):
    VIEW = "VIEW"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    DISMISS = "DISMISS"
    SNOOZE = "SNOOZE"
    CONFIGURE = "CONFIGURE"
    PROVIDE_INPUT = "PROVIDE_INPUT"


class Interaction(Value):
    id: str
    execution_id: str
    user_id: str
    kind: Literal["information", "approval", "input_request", "warning", "result"]
    source_kind: str
    source_id: str
    message: str
    actions: tuple[InteractionAction, ...]
    state: Literal["open", "dismissed", "resolved"]
    created_at: float
    snoozed_until: float | None = None


class NotificationDelivery(Value):
    id: str
    notification_id: str
    interaction_id: str
    channel: Literal["in_app", "console"]
    message: str
    token: str
