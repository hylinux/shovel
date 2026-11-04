from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, Protocol

import structlog
from pydantic import Field, JsonValue

from shovel.capability.domain import FunctionImplementation, Identifier, Permission, Value
from shovel.domain.safe_data import ensure_no_credentials
from shovel.execution.domain import Execution, ExecutionLease, ExecutorResult
from shovel.execution.ports import ExecutorContext
from shovel.execution.validation import validate_data
from shovel.protection.domain import ProtectionDeniedError, content_digest
from shovel.protection.egress import EgressChannel, require_ordinary, require_safe_completion
from shovel.runtime.errors import RuntimeFailure
from shovel.security.domain import SecurityDenied

if TYPE_CHECKING:
    from shovel.capability.domain import CapabilityScope
    from shovel.security.ports import ExecutionGate

type EffectState = Literal["intent", "dispatched", "uncertain", "not_sent", "not_applied", "applied"]
UNRESOLVED = ("intent", "dispatched", "uncertain")


class EffectIntent(Value):
    """Only hashes cross the journal boundary; payloads and credentials stay in the adapter."""

    implementation_id: Identifier
    contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    binding_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump(mode="json"))


class EffectEvidence(Value):
    outcome: Literal["applied", "not_applied", "unknown"]
    evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class EffectReceipt(Value):
    id: str
    execution_id: str
    intent: EffectIntent
    state: EffectState
    revision: int
    attempt: int
    resume_required: bool
    conflicted: bool = False
    evidence_digest: str | None = None


class ExternalEffectUncertain(RuntimeFailure):
    def __init__(self) -> None:
        super().__init__("External effect requires reconciliation and explicit resume.", status=409)


class ExternalEffectAdapter(Protocol):
    """Installed code, never a draft flag or a user-supplied import.

    prepare is side-effect-free. authorize rechecks the exact current target, identity,
    source and policy, including independent egress permission, immediately before send.
    send performs at most one wire attempt, without SDK/HTTP/authentication retries.
    reconcile is read-only. not_applied MUST prove the old request cannot apply later;
    a missing lookup result, timeout or expired dedupe key is not that proof.
    """

    permission: Permission
    completion_output: dict[str, JsonValue]

    async def prepare(self, execution: Execution) -> EffectIntent: ...
    async def authorize(self, lease: ExecutionLease, intent: EffectIntent) -> None: ...
    async def send(
        self, execution: Execution, intent: EffectIntent, *, idempotency_key: str
    ) -> EffectEvidence: ...
    async def authorize_reconciliation(self, execution: Execution, receipt: EffectReceipt) -> None: ...
    async def reconcile(
        self, execution: Execution, receipt: EffectReceipt, *, idempotency_key: str
    ) -> EffectEvidence: ...


class ExternalEffectRepository(Protocol):
    async def get_execution(self, scope: CapabilityScope, identifier: str) -> Execution: ...
    async def prepare(
        self, lease: ExecutionLease, intent: EffectIntent, gate: ExecutionGate
    ) -> tuple[EffectReceipt, str]: ...
    async def dispatch(self, lease: ExecutionLease, identifier: str, gate: ExecutionGate) -> None: ...
    async def not_sent(self, lease: ExecutionLease, identifier: str) -> bool: ...
    async def received(
        self, lease: ExecutionLease, identifier: str, evidence: EffectEvidence
    ) -> EffectReceipt: ...
    async def inspect(
        self, scope: CapabilityScope, identifier: str
    ) -> tuple[Execution, list[EffectReceipt]]: ...
    async def claim_reconciliation(
        self, scope: CapabilityScope, identifier: str, receipt: EffectReceipt
    ) -> tuple[str, str]: ...
    async def reconciled(
        self,
        scope: CapabilityScope,
        identifier: str,
        effect_id: str,
        token: str,
        evidence: EffectEvidence,
    ) -> None: ...
    async def resume(self, scope: CapabilityScope, identifier: str) -> Execution: ...


class ExternalEffectService:
    def __init__(
        self,
        repository: ExternalEffectRepository,
        gate: ExecutionGate,
        adapters: Mapping[str, ExternalEffectAdapter],
    ) -> None:
        self.repository, self.gate = repository, gate
        self._adapters = dict(adapters)
        self._log = structlog.get_logger("shovel.execution.effects")

    def _adapter(self, execution: Execution) -> ExternalEffectAdapter:
        content = execution.snapshot.capability.content
        implementation = content.implementation
        if not isinstance(implementation, FunctionImplementation) or content.side_effect_profile.kind not in {
            "external_write",
            "destructive",
        }:
            raise SecurityDenied("External effects require an installed Function adapter.")
        adapter = self._adapters.get(implementation.implementation_id)
        if (
            adapter is None
            or adapter.permission not in content.required_permissions
            or adapter.permission not in execution.snapshot.granted_permissions
        ):
            raise SecurityDenied("External effect adapter or exact write permission is unavailable.")
        return adapter

    async def _intent(self, execution: Execution, adapter: ExternalEffectAdapter) -> EffectIntent:
        intent = EffectIntent.model_validate((await adapter.prepare(execution)).model_dump(mode="json"))
        implementation = execution.snapshot.capability.content.implementation
        if (
            not isinstance(implementation, FunctionImplementation)
            or intent.implementation_id != implementation.implementation_id
        ):
            raise SecurityDenied("External effect differs from the frozen Function.")
        return intent

    async def execute(self, context: ExecutorContext) -> ExecutorResult:
        lease = context.lease
        execution = context.execution
        if (
            lease is None
            or execution.id != lease.execution.id
            or execution.snapshot != lease.execution.snapshot
            or content_digest(execution.input) != content_digest(lease.execution.input)
        ):
            raise SecurityDenied("The exact owned Execution lease and frozen input are required.")
        adapter = self._adapter(execution)
        completion = ExecutorResult.model_validate({"output": adapter.completion_output})
        validate_data(execution.snapshot.capability.content.output_schema, completion.output)
        ensure_no_credentials(completion)
        require_safe_completion(completion.output)
        intent = await self._intent(execution, adapter)
        receipt, key = await self.repository.prepare(lease, intent, self.gate)
        if receipt.state == "applied":
            return completion
        if receipt.state != "intent":
            raise ExternalEffectUncertain()
        try:
            await adapter.authorize(lease, intent)
            if await self._intent(execution, adapter) != intent:
                raise SecurityDenied("External effect bindings changed before sending.")
            require_ordinary(EgressChannel.TOOL, execution.input)
            await self.repository.dispatch(lease, receipt.id, self.gate)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Only this pre-wire branch can attest that this invocation did not send.
            if not await self.repository.not_sent(lease, receipt.id):
                raise ExternalEffectUncertain() from None
            raise
        try:
            evidence = EffectEvidence.model_validate(
                (await adapter.send(execution, intent, idempotency_key=key)).model_dump(mode="json")
            )
            received = await self.repository.received(lease, receipt.id, evidence)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._log.warning(
                "external_effect.response_uncertain",
                execution_id=execution.id,
                error_type=type(error).__name__,
            )
            raise ExternalEffectUncertain() from None
        if received.state != "applied":
            raise ExternalEffectUncertain()
        return completion

    async def preview(self, scope: CapabilityScope, identifier: str) -> dict[str, object]:
        execution, receipts = await self.repository.inspect(scope, identifier)
        await self.gate.evaluate(execution)
        return {
            "execution_id": execution.id,
            "execution_revision": execution.revision,
            "effects": [row.model_dump(mode="json") for row in receipts],
        }

    async def reconcile(self, scope: CapabilityScope, identifier: str) -> dict[str, object]:
        execution, receipts = await self.repository.inspect(scope, identifier)
        await self.gate.evaluate(execution)
        for receipt in receipts:
            if receipt.state not in UNRESOLVED:
                continue
            owner = await self.repository.get_execution(scope, receipt.execution_id)
            await self.gate.evaluate(owner)
            adapter = self._adapter(owner)
            if await self._intent(owner, adapter) != receipt.intent:
                raise SecurityDenied("Reconciliation requires the original, still-trusted bindings.")
            await adapter.authorize_reconciliation(owner, receipt)
            token, key = await self.repository.claim_reconciliation(scope, identifier, receipt)
            try:
                evidence = EffectEvidence.model_validate(
                    (
                        await asyncio.wait_for(
                            adapter.reconcile(owner, receipt, idempotency_key=key), timeout=30
                        )
                    ).model_dump(mode="json")
                )
                await self.gate.evaluate(owner)
                await adapter.authorize_reconciliation(owner, receipt)
                if await self._intent(owner, adapter) != receipt.intent:
                    raise SecurityDenied("Reconciliation bindings changed.")
                await self.repository.reconciled(scope, identifier, receipt.id, token, evidence)
            except asyncio.CancelledError:
                self._log.warning("external_effect.reconciliation_interrupted", effect_id=receipt.id)
                raise
            except SecurityDenied, ProtectionDeniedError:
                self._log.warning("external_effect.reconciliation_denied", effect_id=receipt.id)
                raise SecurityDenied("External-effect reconciliation is no longer authorized.") from None
            except Exception as error:
                # The durable claim expires; neither cancellation nor a failed probe is proof of no effect.
                self._log.warning(
                    "external_effect.reconciliation_incomplete",
                    effect_id=receipt.id,
                    error_type=type(error).__name__,
                )
                raise RuntimeFailure(
                    "External-effect reconciliation failed; the outcome remains unresolved.", status=502
                ) from None
        return await self.preview(scope, execution.id)

    async def resume(self, scope: CapabilityScope, identifier: str) -> Execution:
        execution, _ = await self.repository.inspect(scope, identifier)
        await self.gate.evaluate(execution)
        return await self.repository.resume(scope, identifier)
