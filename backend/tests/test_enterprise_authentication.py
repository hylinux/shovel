import asyncio
import time
import traceback

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from shovel.capability.domain import CapabilityScope
from shovel.cli.app import app
from shovel.db.authentication_repository import SQLiteAuthenticationRepository
from shovel.db.enterprise_repository import SQLiteConnectionRepository
from shovel.db.identity_repository import SQLiteIdentityRepository
from shovel.db.interaction_repository import SQLiteInteractionRepository
from shovel.enterprise.catalog import InstalledProviderCatalog
from shovel.enterprise.service import ConnectionService
from shovel.enterprise.transport import ProviderRequest
from shovel.identity.domain import (
    AuthContext,
    AuthenticationMode,
    AuthenticationResolution,
    AuthenticationState,
    BrokerType,
    IdentityBinding,
    IdentityDraft,
    IdentityProfile,
)
from shovel.identity.errors import (
    AuthConfigurationError,
    AuthenticationDeadlineError,
    AuthenticationRequiredError,
    BrokerUnavailableError,
)
from shovel.identity.profiles import IdentityService
from shovel.identity.service import (
    AuthenticationService,
    UnavailableAuthenticationResolver,
    UnavailableWindowsBroker,
)
from shovel.platform.domain import PlatformCommand
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.runtime.service import Runtime
from shovel.security.domain import SecurityDenied
from test_capability_registry import SCOPE, Catalog
from test_capability_registry import catalog as catalog
from test_data_protection_contracts import connection_binding
from test_enterprise_connection_contracts import manifest, resource
from test_enterprise_connections import draft as connection_draft
from test_enterprise_identity_contracts import TENANT, token_request
from test_execution_engine import Kernel, invocation, publish
from test_execution_engine import kernel as kernel
from test_hosted_runtime import api as api
from test_hosted_runtime import runtime as runtime


def profile() -> IdentityProfile:
    return IdentityProfile(
        id="work",
        display_name="Test",
        tenant_binding=TENANT,
        broker_type=BrokerType.PYTHON_WAM,
        enabled=True,
        created_at=1,
        updated_at=1,
    )


class Resolver:
    def __init__(self, deadline: float = 20) -> None:
        self.resolution = AuthenticationResolution(
            approved_request=token_request(),
            profile=profile(),
            deadline_at=deadline,
        )

    async def resolve(self, request):
        return self.resolution


class Broker:
    def __init__(self) -> None:
        self.calls = 0
        self.context = None

    async def acquire(self, request):
        self.calls += 1
        self.context = AuthContext(request, SecretStr("AUTH-SENTINEL"), expires_at=20)
        return self.context

    async def availability(self, profile):
        return AuthenticationState.READY_SILENT


async def test_silent_auth_context_is_exact_and_one_use() -> None:
    broker = Broker()
    service = AuthenticationService(Resolver(), broker, clock=lambda: 10)
    context = await service.authenticate(token_request())
    assert broker.calls == 1
    assert context.consume(token_request(), now=10, transport=lambda _: "received") == "received"
    with pytest.raises(ValueError):
        context.validate(token_request(), now=10)


@pytest.mark.parametrize(
    "changes",
    [
        {"interaction_mode": "interactive"},
        {"force_refresh": True},
        {"claims_challenge_ref": "untrusted"},
        {"allowed_scopes": ("write",)},
        {"audience": "other"},
        {"connection_revision": 2},
    ],
)
async def test_unresolved_expansion_never_reaches_broker(changes) -> None:
    broker = Broker()
    service = AuthenticationService(Resolver(), broker, clock=lambda: 10)
    with pytest.raises(AuthConfigurationError):
        await service.authenticate(token_request(**changes))
    assert broker.calls == 0


async def test_stale_profile_after_acquisition_closes_context() -> None:
    resolver = Resolver()

    class ChangingBroker(Broker):
        async def acquire(self, request):
            context = await super().acquire(request)
            resolver.resolution = resolver.resolution.model_copy(update={"deadline_at": 30})
            return context

    broker = ChangingBroker()
    service = AuthenticationService(resolver, broker, clock=lambda: 10)
    with pytest.raises(AuthConfigurationError):
        await service.authenticate(token_request())
    with pytest.raises(ValueError):
        broker.context.validate(token_request(), now=10)


async def test_authentication_errors_do_not_echo_or_chain_sdk_secrets() -> None:
    class FailingBroker(Broker):
        async def acquire(self, request):
            raise RuntimeError("AUTH-SENTINEL")

    with pytest.raises(BrokerUnavailableError) as caught:
        await AuthenticationService(Resolver(), FailingBroker(), clock=lambda: 10).authenticate(
            token_request()
        )
    assert "AUTH-SENTINEL" not in "".join(traceback.format_exception(caught.value))

    class InteractionBroker(Broker):
        async def acquire(self, request):
            raise AuthenticationRequiredError(correlation_id=request.correlation_id) from RuntimeError(
                "AUTH-SENTINEL"
            )

    with pytest.raises(AuthenticationRequiredError) as caught:
        await AuthenticationService(Resolver(), InteractionBroker(), clock=lambda: 10).authenticate(
            token_request()
        )
    assert caught.value.__cause__ is None
    assert "AUTH-SENTINEL" not in "".join(traceback.format_exception(caught.value))


async def test_deadline_returns_promptly_and_closes_cancel_ignoring_late_context() -> None:
    done = asyncio.Event()
    produced = []

    class LateBroker(Broker):
        async def acquire(self, request):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                context = AuthContext(request, SecretStr("AUTH-SENTINEL"), expires_at=time.time() + 20)
                produced.append(context)
                done.set()
                return context

    resolver = Resolver(time.time() + 0.05)
    with pytest.raises(AuthenticationDeadlineError):
        await AuthenticationService(resolver, LateBroker()).authenticate(token_request())
    await asyncio.wait_for(done.wait(), timeout=1)
    await asyncio.sleep(0)
    with pytest.raises(ValueError):
        produced[0].validate(token_request(), now=time.time())


async def test_production_authentication_is_explicitly_unavailable() -> None:
    service = AuthenticationService(UnavailableAuthenticationResolver(), UnavailableWindowsBroker())
    with pytest.raises(BrokerUnavailableError):
        await service.authenticate(token_request())
    assert (
        await service.status(profile().model_copy(update={"enabled": False}))
        == AuthenticationState.UNAVAILABLE
    )


async def test_wrong_broker_context_is_closed_without_releasing_it() -> None:
    class WrongBroker(Broker):
        async def acquire(self, request):
            self.context = AuthContext(
                token_request(connection_revision=2),
                SecretStr("AUTH-SENTINEL"),
                expires_at=20,
            )
            return self.context

    broker = WrongBroker()
    with pytest.raises(AuthConfigurationError):
        await AuthenticationService(Resolver(), broker, clock=lambda: 10).authenticate(token_request())
    with pytest.raises(ValueError):
        broker.context.validate(token_request(connection_revision=2), now=10)


async def test_expired_deadline_never_starts_acquisition_and_status_errors_are_safe() -> None:
    broker = Broker()
    with pytest.raises(AuthenticationDeadlineError):
        await AuthenticationService(Resolver(), broker, clock=lambda: 20).authenticate(token_request())
    assert broker.calls == 0

    class StatusBroker(Broker):
        async def availability(self, profile):
            raise RuntimeError("AUTH-SENTINEL")

    with pytest.raises(BrokerUnavailableError) as caught:
        await AuthenticationService(Resolver(), StatusBroker()).status(profile())
    assert "AUTH-SENTINEL" not in "".join(traceback.format_exception(caught.value))


async def test_context_expiring_during_revalidation_is_closed() -> None:
    now = [10]

    class RevalidatingResolver(Resolver):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def resolve(self, request):
            self.calls += 1
            if self.calls == 2:
                now[0] = 12
            return self.resolution

    class ShortBroker(Broker):
        async def acquire(self, request):
            self.context = AuthContext(request, SecretStr("AUTH-SENTINEL"), expires_at=11)
            return self.context

    broker = ShortBroker()
    with pytest.raises(AuthConfigurationError):
        await AuthenticationService(RevalidatingResolver(), broker, clock=lambda: now[0]).authenticate(
            token_request()
        )
    with pytest.raises(ValueError):
        broker.context.validate(token_request(), now=10)


@pytest.mark.parametrize(
    "mode", [AuthenticationMode.AZURE_CLI_DIAGNOSTIC, AuthenticationMode.DEVICE_CODE_DIAGNOSTIC]
)
async def test_silent_service_does_not_use_diagnostic_authentication_modes(mode) -> None:
    resolver = Resolver()
    resolver.resolution = resolver.resolution.model_copy(
        update={"profile": profile().model_copy(update={"authentication_mode": mode})}
    )
    broker = Broker()
    with pytest.raises(AuthConfigurationError):
        await AuthenticationService(resolver, broker, clock=lambda: 10).authenticate(token_request())
    assert broker.calls == 0


async def test_status_reports_interaction_required_without_acquiring() -> None:
    class InteractionStatusBroker(Broker):
        async def availability(self, profile):
            return AuthenticationState.INTERACTION_REQUIRED

    broker = InteractionStatusBroker()
    assert (
        await AuthenticationService(Resolver(), broker).status(profile())
        == AuthenticationState.INTERACTION_REQUIRED
    )
    assert broker.calls == 0


async def test_caller_cancel_discards_late_result_without_retry() -> None:
    started, finished = asyncio.Event(), asyncio.Event()
    produced = []

    class LateBroker(Broker):
        async def acquire(self, request):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                produced.append(AuthContext(request, SecretStr("AUTH-SENTINEL"), expires_at=20))
                finished.set()
                return produced[0]

    task = asyncio.create_task(
        AuthenticationService(Resolver(), LateBroker(), clock=lambda: 10).authenticate(token_request())
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await finished.wait()
    await asyncio.sleep(0)
    with pytest.raises(ValueError):
        produced[0].validate(token_request(), now=10)


@pytest.mark.parametrize(
    "changes",
    [
        {"connection_revision": 2},
        {"resource": resource(tenant_id="22222222-2222-2222-2222-222222222222")},
        {"headers": {"Authorization": "AUTH-SENTINEL"}},
        {"url": "https://arbitrary.example"},
    ],
)
def test_transport_contract_cannot_override_target_or_headers(changes) -> None:
    values = {
        "operation_id": "read",
        "connection": connection_binding(),
        "authentication": token_request(),
        "resource": resource(),
    }
    if "connection_revision" in changes:
        values["connection"] = connection_binding().model_copy(update=changes)
    else:
        values.update(changes)
    with pytest.raises(ValidationError):
        ProviderRequest.model_validate(values)


async def test_identity_revision_scope_and_unavailable_state(catalog: Catalog) -> None:
    identities = IdentityService(SQLiteIdentityRepository(catalog.sessions))
    draft = IdentityDraft(display_name="Work", tenant_binding=TENANT)
    created = await identities.create(SCOPE, draft)
    assert not created.profile.enabled and created.profile.status == AuthenticationState.UNAVAILABLE
    assert await identities.get(SCOPE, created.profile.id) == created
    updated = await identities.update(SCOPE, created.profile.id, draft, expected_revision=1)
    assert updated.profile.revision == 2
    with pytest.raises(RuntimeFailure):
        await identities.disable(SCOPE, created.profile.id, expected_revision=1)
    with pytest.raises(NotFound):
        await identities.get(CapabilityScope(workspace_id="other", user_id=SCOPE.user_id), created.profile.id)
    assert (await identities.disable(SCOPE, created.profile.id, expected_revision=2)).profile.revision == 3
    with pytest.raises(ValidationError):
        IdentityDraft(display_name="Work", tenant_binding=TENANT, broker_type="python_wam")


async def wait_fixture(kernel: Kernel):
    identities = IdentityService(SQLiteIdentityRepository(kernel.catalog.sessions))
    identity = await identities.create(SCOPE, IdentityDraft(display_name="Work", tenant_binding=TENANT))
    connections = ConnectionService(
        SQLiteConnectionRepository(kernel.catalog.sessions),
        InstalledProviderCatalog((manifest(),), runtime_version="0.1.2"),
    )
    connection = await connections.create(SCOPE, connection_draft(identity_profile_ref=identity.profile.id))
    queued = await kernel.invocations.invoke(invocation(await publish(kernel.catalog)))
    lease = await kernel.engine.start(SCOPE, queued.id)
    suspended = await kernel.engine.suspend(lease)
    request = token_request(
        request_id="wait-request",
        execution_id=queued.id,
        correlation_id=queued.correlation_id,
        connection_profile_id=connection.profile.id,
        manifest_digest=manifest().content_hash,
        identity=IdentityBinding(
            workspace_id=SCOPE.workspace_id,
            user_id=SCOPE.user_id,
            identity_profile_id=identity.profile.id,
            identity_revision=1,
            principal_ref="test-principal",
            tenant_id=TENANT,
        ),
    )
    return (
        SQLiteAuthenticationRepository(kernel.catalog.sessions, clock=lambda: kernel.now[0]),
        SQLiteInteractionRepository(kernel.catalog.sessions, clock=lambda: kernel.now[0]),
        request,
        suspended,
        identities,
    )


async def test_durable_wait_replays_projects_and_preserves_original_deadline(kernel: Kernel) -> None:
    requests, interactions, request, execution, _ = await wait_fixture(kernel)
    created = await requests.create(SCOPE, request, AuthenticationState.INTERACTION_REQUIRED)
    assert created.expires_at == execution.deadline_at
    assert await requests.create(SCOPE, request, AuthenticationState.INTERACTION_REQUIRED) == created
    restarted = SQLiteAuthenticationRepository(kernel.catalog.sessions, clock=lambda: kernel.now[0])
    assert await restarted.get(SCOPE, created.id) == created
    rows = await interactions.list(SCOPE)
    assert len(rows) == 1 and rows[0].source_kind == "authentication_request"
    await interactions.dismiss(SCOPE, rows[0].id)
    assert (await requests.get(SCOPE, created.id)).state == "pending"
    assert (await kernel.repository.get(SCOPE, execution.id)).deadline_at == execution.deadline_at
    with pytest.raises(RuntimeFailure):
        await requests.cancel(SCOPE, created.id, execution_id="other")
    assert (await requests.cancel(SCOPE, created.id, execution_id=execution.id)).state == "cancelled"


async def test_wait_expiry_and_profile_change_close_projection_without_resume(kernel: Kernel) -> None:
    requests, interactions, request, execution, identities = await wait_fixture(kernel)
    created = await requests.create(SCOPE, request, AuthenticationState.CLAIMS_CHALLENGE_REQUIRED)
    assert created.requires_fresh_challenge
    await identities.disable(SCOPE, request.identity.identity_profile_id, expected_revision=1)
    await interactions.refresh()
    assert (await requests.get(SCOPE, created.id)).state == "cancelled"
    assert (await interactions.list(SCOPE))[0].state == "resolved"
    assert (await kernel.repository.get(SCOPE, execution.id)).state == execution.state


async def test_wait_deadline_expiry_is_durable(kernel: Kernel) -> None:
    requests, interactions, request, execution, _ = await wait_fixture(kernel)
    await requests.create(SCOPE, request, AuthenticationState.INTERACTION_REQUIRED)
    kernel.now[0] = execution.deadline_at
    await interactions.refresh()
    assert (await requests.get(SCOPE, request.request_id)).state == "expired"
    with pytest.raises(RuntimeFailure):
        await requests.create(SCOPE, request, AuthenticationState.INTERACTION_REQUIRED)


async def test_wait_transaction_rolls_back_if_projection_fails(kernel: Kernel, monkeypatch) -> None:
    requests, _, request, _, _ = await wait_fixture(kernel)

    async def fail(*args, **kwargs):
        raise RuntimeError("projection failed")

    monkeypatch.setattr("shovel.db.authentication_repository.project", fail)
    with pytest.raises(RuntimeError, match="projection failed"):
        await requests.create(SCOPE, request, AuthenticationState.INTERACTION_REQUIRED)
    assert await requests.list(SCOPE) == []


async def test_wait_strips_transient_claims_and_sql_binding_is_immutable(kernel: Kernel) -> None:
    requests, _, request, execution, _ = await wait_fixture(kernel)
    request = request.model_copy(update={"claims_challenge_ref": "EPHEMERAL-SENTINEL"})
    created = await requests.create(SCOPE, request, AuthenticationState.CLAIMS_CHALLENGE_REQUIRED)
    assert created.request.claims_challenge_ref is None and created.requires_fresh_challenge
    async with kernel.catalog.sessions() as session:
        payload = await session.scalar(text("SELECT request FROM authentication_request"))
        assert "EPHEMERAL-SENTINEL" not in payload
        with pytest.raises(IntegrityError):
            await session.execute(text("UPDATE authentication_request SET expires_at=expires_at+100"))
        await session.rollback()
    with pytest.raises(NotFound):
        await requests.get(CapabilityScope(workspace_id=SCOPE.workspace_id, user_id="other"), created.id)
    assert created.expires_at == execution.deadline_at


async def test_identity_http_is_safe_unavailable_and_agent_cannot_configure(
    runtime: Runtime,
    api: httpx.AsyncClient,
) -> None:
    response = await api.post(
        "/api/v1/platform/commands",
        json={
            "action": "enterprise.identity.create",
            "input": {"display_name": "Work", "tenant_binding": TENANT},
        },
    )
    assert response.status_code == 200
    identifier = response.json()["profile"]["id"]
    status = await api.post(
        "/api/v1/platform/commands",
        json={
            "action": "enterprise.identity.status",
            "identifier": identifier,
        },
    )
    assert status.json()["state"] == "unavailable"
    assert len((await api.get("/api/v1/platform/enterprise/identities")).json()["items"]) == 1
    assert (await api.get("/api/v1/platform/authentication")).json() == {"items": []}
    rejected = await api.post(
        "/api/v1/platform/commands",
        json={
            "action": "enterprise.identity.create",
            "input": {"display_name": "Work", "tenant_binding": TENANT, "AUTH-SENTINEL": "secret"},
        },
    )
    assert rejected.status_code == 422 and "AUTH-SENTINEL" not in rejected.text
    with pytest.raises(SecurityDenied):
        await runtime.platform.execute(PlatformCommand(action="enterprise.identity.list"), human=False)


def test_identity_cli_and_request_cancellation_use_platform(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr("shovel.cli.commands.platform._run", calls.append)
    path = tmp_path / "identity.json"
    path.write_text(
        IdentityDraft(display_name="Work", tenant_binding=TENANT).model_dump_json(), encoding="utf-8"
    )
    runner = CliRunner()
    assert runner.invoke(app, ["identity", "create", str(path)]).exit_code == 0
    assert calls[-1].action == "enterprise.identity.create"
    assert (
        runner.invoke(app, ["authentication", "cancel", "request", "--execution-id", "execution"]).exit_code
        == 0
    )
    assert calls[-1].execution_id == "execution"
