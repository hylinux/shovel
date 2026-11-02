import asyncio
import traceback

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from structlog.testing import capture_logs

from shovel.db.model.enterprise_execution import ProtectionAudit
from shovel.enterprise.domain import Cloud
from shovel.execution.domain import ExecutionState
from shovel.identity.domain import AuthContext
from shovel.identity.errors import AccountNotFoundError, BrokerUnavailableError
from shovel.protection.domain import DataAction, ProtectedContent
from shovel.protection.persistence import SourceChange
from shovel.runtime.diagnostics import development_logging
from shovel.runtime.errors import RuntimeFailure
from test_capability_registry import SCOPE
from test_capability_registry import catalog as catalog
from test_enterprise_execution import BODY, TOKEN, assert_no_durable_body
from test_enterprise_execution import flow as flow
from test_enterprise_identity_contracts import OTHER_TENANT
from test_execution_engine import kernel as kernel


@pytest.mark.parametrize(
    "alter",
    [
        "disabled_connection",
        "cloud",
        "endpoint",
        "provider",
        "scope",
        "connection_revision",
        "disabled_identity",
        "tenant",
        "audience",
    ],
)
async def test_invalid_resolved_target_fails_before_broker_or_business_request(flow, monkeypatch, alter):
    original = flow.plan

    async def invalid(execution):
        plan = await original(execution)
        if alter in {"disabled_connection", "cloud", "provider"}:
            changes = {
                "disabled_connection": {"enabled": False},
                "cloud": {"cloud": Cloud.AZURE_CHINA},
                "provider": {"provider_id": "uninstalled"},
            }[alter]
            return plan.model_copy(update={"profile": plan.profile.model_copy(update=changes)})
        if alter == "disabled_identity":
            return plan.model_copy(update={"identity": plan.identity.model_copy(update={"enabled": False})})
        if alter == "endpoint":
            connection = plan.request.connection.model_copy(update={"endpoint": "https://other.example"})
            request = plan.request.model_copy(update={"connection": connection})
        else:
            changes = {
                "scope": {"allowed_scopes": ("api://resource/write",)},
                "connection_revision": {"connection_revision": 2},
                "tenant": {
                    "identity": plan.request.authentication.identity.model_copy(
                        update={"tenant_id": OTHER_TENANT}
                    )
                },
                "audience": {"audience": "api://other"},
            }[alter]
            request = plan.request.model_copy(
                update={"authentication": plan.request.authentication.model_copy(update=changes)}
            )
        return plan.model_copy(update={"request": request})

    monkeypatch.setattr(flow, "plan", invalid)
    failed = await flow.run(await flow.invoke())
    assert failed.state == ExecutionState.FAILED
    assert flow.broker.calls == flow.transport.calls == 0 and not flow.frames
    assert await flow.requests.list(SCOPE) == []
    await assert_no_durable_body(flow)


@pytest.mark.parametrize("error", [AccountNotFoundError, BrokerUnavailableError])
async def test_missing_account_and_broker_wait_without_polling_or_business_dispatch(flow, error):
    flow.broker.error = error
    execution = await flow.invoke()
    waiting = await flow.run(execution)
    request = (await flow.requests.list(SCOPE))[0]
    assert waiting.wait_reason == "waiting_for_authentication" and request.reason == error.state
    assert await flow.wakeups.run_due() == 0
    assert flow.broker.calls == 1 and flow.transport.calls == 0 and not flow.frames
    await assert_no_durable_body(flow)


@pytest.mark.parametrize("kind", ["delay", "snooze", "resume"])
async def test_duplicate_timers_cannot_replace_authentication_wait_with_automatic_resume(flow, kind):
    flow.broker.error = BrokerUnavailableError
    execution = await flow.invoke()
    waiting = await flow.run(execution)
    control = flow.wakeups.repository
    timers = await asyncio.gather(
        *(control.timer(SCOPE, execution.id, flow.kernel.now[0] + 1, kind) for _ in range(2))
    )
    assert timers[0].id == timers[1].id
    flow.kernel.now[0] += 1
    assert await flow.wakeups.run_due() == 0
    assert await flow.wakeups.run_due() == 0
    current = await flow.kernel.repository.get(SCOPE, execution.id)
    assert current.state == ExecutionState.SUSPENDED
    assert current.wait_reason == "waiting_for_authentication"
    assert current.deadline_at == waiting.deadline_at
    assert flow.broker.calls == 1 and flow.transport.calls == 0 and not flow.frames


async def test_concurrent_manual_resume_and_dispatch_release_only_one_result(flow):
    flow.broker.error = BrokerUnavailableError
    execution = await flow.invoke()
    await flow.run(execution)
    flow.broker.error = None
    resumed = await asyncio.gather(
        *(flow.kernel.engine.resume(SCOPE, execution.id) for _ in range(2)), return_exceptions=True
    )
    assert sum(isinstance(item, RuntimeFailure) for item in resumed) == 1
    results = await asyncio.gather(flow.run(execution), flow.run(execution), return_exceptions=True)
    assert sum(isinstance(item, RuntimeFailure) for item in results) == 1
    assert (await flow.kernel.repository.get(SCOPE, execution.id)).state == ExecutionState.SUCCEEDED
    assert flow.broker.calls == 2 and flow.transport.calls == 1 and len(flow.frames) == 1
    await assert_no_durable_body(flow)


@pytest.mark.parametrize("stage", ["broker", "transport"])
async def test_cancellation_rejects_late_cancel_ignoring_authentication_or_content(flow, monkeypatch, stage):
    entered, finish, late = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def pause():
        entered.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            await finish.wait()

    if stage == "broker":

        async def acquire(request):
            flow.broker.calls += 1
            await pause()
            context = AuthContext(request, SecretStr(TOKEN), expires_at=flow.kernel.now[0] + 60)
            flow.broker.contexts.append((request, context))
            late.set()
            return context

        monkeypatch.setattr(flow.broker, "acquire", acquire)
    else:

        async def response():
            await pause()
            late.set()

        flow.transport.before_response = response
    execution = await flow.invoke()
    task = asyncio.create_task(flow.run(execution))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert (await flow.kernel.engine.cancel(SCOPE, execution.id)).state == ExecutionState.CANCELLED
    finally:
        finish.set()
    assert (await asyncio.wait_for(task, 10)).state == ExecutionState.CANCELLED
    await asyncio.wait_for(late.wait(), 10)
    await asyncio.sleep(0)
    for request, context in flow.broker.contexts:
        with pytest.raises(ValueError):
            context.validate(request, now=flow.kernel.now[0])
    assert flow.transport.calls == (1 if stage == "transport" else 0) and not flow.frames
    assert await flow.requests.list(SCOPE) == []
    await assert_no_durable_body(flow)


@pytest.mark.parametrize("alter", ["missing_metadata", "unknown_rights", "unknown_protection"])
async def test_incomplete_protection_is_not_downgraded_to_ordinary_content(flow, monkeypatch, alter):
    original = flow.decode

    def decode(plan, response):
        content = original(plan, response)
        if alter == "missing_metadata":
            return BODY
        changes = {"rights": None} if alter == "unknown_rights" else {"protection_status": "unknown"}
        context = content.metadata.protection_context.model_copy(update=changes)
        return ProtectedContent(BODY, content.metadata.model_copy(update={"protection_context": context}))

    monkeypatch.setattr(flow, "decode", decode)
    assert (await flow.run(await flow.invoke())).state == ExecutionState.FAILED
    assert not flow.frames
    await assert_no_durable_body(flow)


@pytest.mark.parametrize("alter", ["stale", "denied", "policy_changed"])
async def test_current_source_evidence_must_authorize_output_not_merely_prior_read(flow, monkeypatch, alter):
    original = flow.data_policy.resolve

    def resolve(context, binding):
        state = original(context, binding)
        if alter == "policy_changed":
            return state.model_copy(
                update={"context": context.model_copy(update={"policy_version": "f" * 64})}
            )
        source = state.sources[0]
        snapshot = source.snapshot.model_copy(
            update={"freshness": "stale"} if alter == "stale" else {"decision": "deny"}
        )
        return state.model_copy(update={"sources": (source.model_copy(update={"snapshot": snapshot}),)})

    monkeypatch.setattr(flow.data_policy, "resolve", resolve)
    failed = await flow.run(await flow.invoke())
    assert failed.error == "data_use_denied" and not flow.frames
    async with flow.kernel.catalog.sessions() as session:
        audits = list(await session.scalars(select(ProtectionAudit.event)))
    assert len(audits) == 1 and audits[0]["outcome"] != "allow"
    await assert_no_durable_body(flow)


async def test_changed_policy_requires_new_invocation_never_revives_a_terminal_denial(flow):
    flow.data_policy.allowed.clear()
    execution = await flow.invoke()
    denied = await flow.run(execution)
    assert denied.error == "data_use_denied"
    flow.data_policy.allowed.update({DataAction.READ, DataAction.DISPLAY})
    with pytest.raises(RuntimeFailure):
        await flow.kernel.engine.resume(SCOPE, execution.id)
    assert await flow.wakeups.run_due() == 0
    assert (await flow.run(await flow.invoke())).state == ExecutionState.SUCCEEDED
    assert len(flow.frames) == 1 and flow.frames[0][0] != execution.id


@pytest.mark.parametrize("stage", ["broker", "decoder", "display"])
async def test_sdk_and_content_sentinels_do_not_enter_diagnostics_reprs_or_sqlite(
    flow,
    monkeypatch,
    capsys,
    caplog,
    stage,
):
    original = flow.decode
    envelopes = []

    def decode(plan, response):
        envelope = original(plan, response)
        envelopes.append(envelope)
        if stage == "decoder":
            raise RuntimeError(f"{TOKEN}: {BODY}")
        return envelope

    async def sdk_failure():
        raise RuntimeError(f"{TOKEN}: {BODY}")

    monkeypatch.setattr(flow, "decode", decode)
    flow.broker.before_return = sdk_failure if stage == "broker" else None
    flow.display_error = stage == "display"
    with development_logging(), capture_logs() as logs:
        result = await flow.run(await flow.invoke())
    assert logs
    output = capsys.readouterr()
    representations = [
        str(logs),
        caplog.text,
        output.out,
        output.err,
        result.model_dump_json(),
        *(repr(item) for item in envelopes),
        *(repr(context) for _, context in flow.broker.contexts),
    ]
    for serialized in representations:
        assert TOKEN not in serialized and "ENTERPRISE-BODY-SENTINEL" not in serialized
    assert "Traceback" not in str(logs)
    await assert_no_durable_body(flow)


async def test_broker_exception_traceback_is_safe_without_engine_swallowing_it(flow):
    async def fail():
        raise RuntimeError(f"{TOKEN}: {BODY}")

    flow.broker.before_return = fail
    execution = await flow.invoke()
    plan = await flow.plan(execution)
    # A running execution normally supplies the deadline; no network or Engine dispatch here.
    flow.executions[execution.id] = execution.model_copy(update={"deadline_at": flow.kernel.now[0] + 60})
    with pytest.raises(BrokerUnavailableError) as caught:
        await flow.authentication.authenticate(plan.request.authentication)
    formatted = "".join(traceback.format_exception(caught.value))
    assert TOKEN not in formatted and "ENTERPRISE-BODY-SENTINEL" not in formatted


async def test_source_deleted_while_authentication_waits_cannot_reappear_on_resume(flow):
    flow.broker.error = BrokerUnavailableError
    execution = await flow.invoke()
    waiting = await flow.run(execution)
    plan = await flow.plan(execution)
    await flow.protection.invalidate(
        SourceChange(
            event_id="verified-deletion",
            scope=SCOPE,
            resource=plan.source.resource,
            expected_generation=0,
            reason="source_deleted",
        )
    )
    await flow.restart()
    flow.broker.error = None
    await flow.kernel.engine.resume(SCOPE, execution.id)
    failed = await flow.run(execution)
    assert failed.error == "data_use_denied" and failed.deadline_at == waiting.deadline_at
    assert flow.broker.calls == 1 and flow.transport.calls == 0 and not flow.frames
    assert (await flow.requests.list(SCOPE))[0].state == "cancelled"
    await assert_no_durable_body(flow)
