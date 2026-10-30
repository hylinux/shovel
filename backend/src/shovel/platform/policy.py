from collections.abc import Callable

from shovel.capability.domain import CapabilityScope
from shovel.security.domain import SecurityDenied, UserSecurityPolicy


class ConfiguredSecurityPolicy:
    def __init__(self, scope: CapabilityScope, policy: Callable[[], UserSecurityPolicy]) -> None:
        self.scope, self.policy = scope, policy

    async def load(self, scope: CapabilityScope) -> UserSecurityPolicy:
        if scope != self.scope:
            raise SecurityDenied("The local platform owner does not match this scope.")
        return self.policy()
