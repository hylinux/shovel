from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from shovel.capability.domain import CapabilityScope
from shovel.security.domain import SecurityDenied


class ExplicitActions:
    def __init__(self) -> None:
        self._intent: ContextVar[tuple[str, ...]] = ContextVar("platform_explicit_intent", default=())

    @contextmanager
    def allow(self, scope: CapabilityScope, *intent: str) -> Iterator[None]:
        token = self._intent.set((scope.workspace_id, scope.user_id, *intent))
        try:
            yield
        finally:
            self._intent.reset(token)

    def check(self, scope: CapabilityScope, *intent: str) -> None:
        if self._intent.get() != (scope.workspace_id, scope.user_id, *intent):
            raise SecurityDenied("This action requires explicit, structured user confirmation.")


class PublishAuthorization:
    def __init__(self, actions: ExplicitActions) -> None:
        self.actions = actions

    async def authorize(
        self,
        scope: CapabilityScope,
        action: Literal["publish", "availability"],
        capability_id: str,
        version: int | None,
        content_hash: str,
    ) -> None:
        self.actions.check(scope, action, capability_id, str(version), content_hash)


class ApprovalAuthorization:
    def __init__(self, actions: ExplicitActions) -> None:
        self.actions = actions

    async def authorize(
        self,
        scope: CapabilityScope,
        user_id: str,
        execution_id: str,
        request_id: str,
        action: Literal["approve", "reject"],
    ) -> None:
        self.actions.check(scope, action, execution_id, request_id, user_id)


class InputAuthorization:
    def __init__(self, actions: ExplicitActions) -> None:
        self.actions = actions

    async def authorize(
        self, scope: CapabilityScope, execution_id: str, interaction_id: str, user_id: str
    ) -> None:
        self.actions.check(scope, "input", execution_id, interaction_id, user_id)
