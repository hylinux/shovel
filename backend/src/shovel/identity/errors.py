from typing import ClassVar

from pydantic import ValidationError

from shovel.capability.domain import Identifier
from shovel.identity.domain import AuthenticationState, IdentityValue


class ErrorReferences(IdentityValue):
    correlation_id: Identifier
    diagnostic_ref: Identifier | None = None


class AuthenticationError(Exception):
    code: ClassVar[str] = "authentication_unavailable"
    message: ClassVar[str] = "Enterprise authentication is unavailable."
    state: ClassVar[AuthenticationState] = AuthenticationState.UNAVAILABLE
    retryable: ClassVar[bool] = False
    user_action_required: ClassVar[bool] = True

    def __init__(self, *, correlation_id: str, diagnostic_ref: str | None = None) -> None:
        self.references = ErrorReferences(correlation_id=correlation_id, diagnostic_ref=diagnostic_ref)
        super().__init__(self.message)

    def safe_dump(self) -> dict[str, str | bool | None]:
        return {
            "code": self.code,
            "message": self.message,
            "state": self.state.value,
            "retryable": self.retryable,
            "user_action_required": self.user_action_required,
            "correlation_id": self.references.correlation_id,
            "diagnostic_ref": self.references.diagnostic_ref,
        }


class AuthenticationRequiredError(AuthenticationError):
    code = "authentication_required"
    message = "Sign in to continue."
    state = AuthenticationState.INTERACTION_REQUIRED


class ReauthenticationRequiredError(AuthenticationError):
    code = "reauthentication_required"
    message = "Sign in again to continue."
    state = AuthenticationState.REAUTHENTICATION_REQUIRED


class ClaimsChallengeError(AuthenticationError):
    code = "claims_challenge_required"
    message = "Additional authentication requirements must be satisfied."
    state = AuthenticationState.CLAIMS_CHALLENGE_REQUIRED


class WrongTenantError(AuthenticationError):
    code = "wrong_tenant"
    message = "The selected identity does not match the required tenant."
    state = AuthenticationState.CONNECTION_MISCONFIGURED


class TokenAudienceMismatchError(AuthenticationError):
    code = "token_audience_mismatch"
    message = "Authentication does not match the requested resource."
    state = AuthenticationState.CONNECTION_MISCONFIGURED


class AccountNotFoundError(AuthenticationError):
    code = "account_not_found"
    message = "The selected enterprise account is unavailable."
    state = AuthenticationState.ACCOUNT_UNAVAILABLE


class BrokerUnavailableError(AuthenticationError):
    code = "broker_unavailable"
    retryable = True
    user_action_required = False


class ConditionalAccessBlockedError(AuthenticationError):
    code = "conditional_access_blocked"
    message = "Organizational access policy blocks this operation."
    state = AuthenticationState.DEVICE_OR_POLICY_BLOCKED


class DeviceComplianceRequiredError(ConditionalAccessBlockedError):
    code = "device_compliance_required"
    message = "The device must satisfy organizational compliance requirements."


class ConsentRequiredError(AuthenticationError):
    code = "consent_required"
    message = "Organizational consent is required."
    state = AuthenticationState.AUTHORIZATION_REQUIRED


class EntitlementRequiredError(ConsentRequiredError):
    code = "entitlement_required"
    message = "Resource entitlement is required."


class JitActivationRequiredError(ConsentRequiredError):
    code = "jit_activation_required"
    message = "An authorized role must be activated."


class ExternalAuthorizationDeniedError(ConsentRequiredError):
    code = "external_authorization_denied"
    message = "The external resource denies access."
    user_action_required = False


class AuthConfigurationError(AuthenticationError):
    code = "auth_configuration_invalid"
    message = "Enterprise authentication configuration requires correction."
    state = AuthenticationState.CONNECTION_MISCONFIGURED


class AuthenticationDeadlineError(AuthenticationError):
    code = "authentication_deadline_exceeded"
    message = "The existing execution authentication deadline has elapsed."
    user_action_required = False


def safe_validation_errors(error: ValidationError) -> tuple[str, ...]:
    # Field names and validator context can themselves contain untrusted input.
    return tuple(
        issue["type"] for issue in error.errors(include_input=False, include_context=False, include_url=False)
    )
