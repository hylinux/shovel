import asyncio
import time
from collections.abc import Callable

import structlog
from pydantic import ValidationError

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
    AuthenticationError,
    BrokerUnavailableError,
)
from shovel.identity.ports import AuthenticationResolver, WindowsAuthenticationBroker


class UnavailableWindowsBroker:
    async def availability(self, profile: IdentityProfile) -> AuthenticationState:
        return AuthenticationState.UNAVAILABLE

    async def acquire(self, request: TokenRequest) -> AuthContext:
        raise BrokerUnavailableError(correlation_id=request.correlation_id)

    async def cancel(self, request_id: str) -> None:
        raise BrokerUnavailableError(correlation_id=request_id)

    async def disconnect(self, profile: IdentityProfile) -> None:
        raise BrokerUnavailableError(correlation_id=profile.id)


class UnavailableAuthenticationResolver:
    async def resolve(self, request: TokenRequest) -> AuthenticationResolution:
        raise BrokerUnavailableError(correlation_id=request.correlation_id)


class AuthenticationService:
    """One silent acquisition, with trusted binding revalidation and bounded late-result cleanup."""

    def __init__(
        self,
        resolver: AuthenticationResolver,
        broker: WindowsAuthenticationBroker,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.resolver, self.broker, self.clock = resolver, broker, clock

    def _check(self, request: TokenRequest, resolution: AuthenticationResolution) -> None:
        request.validate_ceiling(resolution.approved_request, refresh_allowed=resolution.refresh_allowed)
        if not resolution.profile.enabled or resolution.profile.broker_type == BrokerType.UNAVAILABLE:
            raise BrokerUnavailableError(correlation_id=request.correlation_id)
        if resolution.profile.authentication_mode != AuthenticationMode.WAM:
            raise AuthConfigurationError(correlation_id=request.correlation_id)
        if not 0 <= self.clock() < resolution.deadline_at:
            raise AuthenticationDeadlineError(correlation_id=request.correlation_id)

    @staticmethod
    def _discard(task: asyncio.Task[AuthContext]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            structlog.get_logger("shovel.identity").warning(
                "authentication.late_failure",
                error_type=type(error).__name__,
            )
        else:
            result = task.result()
            if isinstance(result, AuthContext):
                result.close()

    async def authenticate(self, request: TokenRequest) -> AuthContext:
        request = TokenRequest.model_validate(request)
        task: asyncio.Task[AuthContext] | None = None
        context: AuthContext | None = None
        released = False
        try:
            resolution = AuthenticationResolution.model_validate(await self.resolver.resolve(request))
            self._check(request, resolution)
            task = asyncio.create_task(self.broker.acquire(request))
            ready, _ = await asyncio.wait({task}, timeout=resolution.deadline_at - self.clock())
            if not ready:
                raise AuthenticationDeadlineError(correlation_id=request.correlation_id)
            result = task.result()
            if not isinstance(result, AuthContext):
                raise AuthConfigurationError(correlation_id=request.correlation_id)
            context = result
            context.validate(request, now=self.clock())
            current = AuthenticationResolution.model_validate(await self.resolver.resolve(request))
            self._check(request, current)
            if current != resolution:
                raise AuthConfigurationError(correlation_id=request.correlation_id)
            context.validate(request, now=self.clock())
            released = True
            return context
        except AuthenticationError as error:
            if context is not None:
                context.close()
            raise type(error)(
                correlation_id=request.correlation_id,
                diagnostic_ref=error.references.diagnostic_ref,
            ) from None
        except asyncio.CancelledError:
            if context is not None:
                context.close()
            raise
        except ValueError, ValidationError:
            if context is not None:
                context.close()
            raise AuthConfigurationError(correlation_id=request.correlation_id) from None
        except Exception as error:
            if context is not None:
                context.close()
            structlog.get_logger("shovel.identity").warning(
                "authentication.broker_failure",
                error_type=type(error).__name__,
                correlation_id=request.correlation_id,
            )
            raise BrokerUnavailableError(correlation_id=request.correlation_id) from None
        finally:
            if task is not None and not released:
                if task.done():
                    self._discard(task)
                else:
                    task.cancel()
                    task.add_done_callback(self._discard)

    async def status(self, profile: IdentityProfile) -> AuthenticationState:
        profile = IdentityProfile.model_validate(profile)
        if not profile.enabled or profile.broker_type == BrokerType.UNAVAILABLE:
            return AuthenticationState.UNAVAILABLE
        try:
            state = await self.broker.availability(profile)
        except AuthenticationError as error:
            raise type(error)(
                correlation_id=profile.id,
                diagnostic_ref=error.references.diagnostic_ref,
            ) from None
        except Exception as error:
            structlog.get_logger("shovel.identity").warning(
                "authentication.status_failure",
                error_type=type(error).__name__,
                identity_profile_id=profile.id,
            )
            raise BrokerUnavailableError(correlation_id=profile.id) from None
        if not isinstance(state, AuthenticationState):
            raise AuthConfigurationError(correlation_id=profile.id)
        return state
