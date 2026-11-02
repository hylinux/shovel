import time
from collections.abc import Callable
from uuid import uuid4

import structlog
from pydantic import JsonValue

from shovel.protection.domain import (
    DataAction,
    DataUseBinding,
    DependencyKind,
    Lineage,
    LineageEdge,
    ObligationProof,
    PolicyDecision,
    ProtectedContent,
    ProtectedContentMetadata,
    ProtectedContentRef,
    ProtectionAdapterError,
    ProtectionContext,
    ProtectionDeniedError,
    TransientContentHandle,
    content_digest,
)
from shovel.protection.policy import ProtectionPolicy


def _invoke[T, R](callback: Callable[[T], R], value: T) -> R:
    try:
        return callback(value)
    except Exception as error:
        structlog.get_logger("shovel.protection").warning(
            "protection.content_adapter_failure",
            error_type=type(error).__name__,
        )
        raise ProtectionAdapterError(
            "Protected adapter failed; uncertain external effects must not be retried automatically."
        ) from None


class ConservativeProtectionMerge:
    """Same-label policy only; never infer an organization's label order or cross-tenant policy."""

    def __init__(self, operation: str, *, clock: Callable[[], float] = time.time) -> None:
        self.operation, self.clock = operation, clock

    def merge(self, contexts: tuple[ProtectionContext, ...], lineage: Lineage) -> ProtectionContext:
        if not contexts:
            raise ProtectionDeniedError("Derived protection requires at least one source context.")
        contexts = tuple(ProtectionContext.model_validate(item) for item in contexts)
        lineage = Lineage.model_validate(lineage)
        first = contexts[0]
        now = self.clock()
        variable_fields = {
            "id",
            "sources",
            "rights",
            "restrictions",
            "expires_at",
            "revalidate_at",
            "derived",
            "derivation_operation",
            "delete_with_source",
        }
        expected = first.model_dump(exclude=variable_fields)
        sources = set(source for item in contexts for source in item.sources)
        if set(lineage.sources) != sources or len({source.resource.tenant_id for source in sources}) != 1:
            raise ProtectionDeniedError("Merge requires complete same-tenant DATA and CONTROL lineage.")
        rights = set(first.rights or ())
        for item in contexts:
            item.require_known(now)
            if item.model_dump(exclude=variable_fields) != expected:
                raise ProtectionDeniedError(
                    "Classification or protection merge requires an explicit reviewed policy."
                )
            rights.intersection_update(item.rights or ())
        expiry = [item.expires_at for item in contexts if item.expires_at is not None]
        revalidation = [item.revalidate_at for item in contexts if item.revalidate_at is not None]
        return ProtectionContext.model_validate(
            {
                **first.model_dump(),
                "id": uuid4().hex,
                "sources": lineage.sources,
                "rights": tuple(sorted(rights)),
                "restrictions": tuple(sorted({value for item in contexts for value in item.restrictions})),
                "expires_at": min(expiry) if expiry else None,
                "revalidate_at": min(revalidation) if revalidation else None,
                "delete_with_source": any(item.delete_with_source for item in contexts),
                "derived": True,
                "derivation_operation": self.operation,
            }
        )


class ProtectionService:
    """Whole-buffer, trusted-adapter boundary; never forwards incremental output."""

    def __init__(self, policy: ProtectionPolicy, *, clock: Callable[[], float] = time.time) -> None:
        self.policy, self.clock = policy, clock
        self.process_instance_id = uuid4().hex

    def _check_content[T](self, content: ProtectedContent[T]) -> None:
        now = self.clock()
        content.metadata.reference.require_available(
            available=content.metadata.lifecycle_state == "active",
            now=now,
            process_instance_id=self.process_instance_id,
        )
        content.metadata.protection_context.require_known(now)

    def use[T, R](
        self,
        content: ProtectedContent[T],
        binding: DataUseBinding,
        consumer: Callable[[T], R],
        *,
        proofs: tuple[ObligationProof, ...] = (),
    ) -> R:
        self._check_content(content)
        permit = self.policy.permit(content.metadata.protection_context, binding, proofs=proofs)
        value = content.release(permit, binding, now=self.clock())
        self._check_content(content)
        return _invoke(consumer, value)

    def use_decision[T, R](
        self,
        content: ProtectedContent[T],
        binding: DataUseBinding,
        decision: PolicyDecision,
        consumer: Callable[[T], R],
        *,
        proofs: tuple[ObligationProof, ...] = (),
    ) -> R:
        self._check_content(content)
        if decision.binding != binding:
            raise ProtectionDeniedError("Persisted decision differs from the intended data use.")
        permit = self.policy.authorize(decision, proofs=proofs)
        value = content.release(permit, binding, now=self.clock())
        self._check_content(content)
        return _invoke(consumer, value)

    def derive(
        self,
        inputs: tuple[tuple[ProtectedContent[JsonValue], DataUseBinding, DependencyKind], ...],
        operation: str,
        transform: Callable[[tuple[JsonValue, ...]], JsonValue],
    ) -> ProtectedContent[JsonValue]:
        if not inputs:
            raise ProtectionDeniedError("Derivation requires protected inputs.")
        contexts = tuple(item.metadata.protection_context for item, _, _ in inputs)
        anchor = inputs[0][1]
        for content, binding, _ in inputs:
            self._check_content(content)
            if (
                binding.action
                not in {DataAction.PROCESS, DataAction.TRANSFORM, DataAction.SUMMARIZE, DataAction.EXTRACT}
                or (binding.execution_id, binding.step_id, binding.actor_id, binding.identities)
                != (
                    anchor.execution_id,
                    anchor.step_id,
                    anchor.actor_id,
                    anchor.identities,
                )
                or content.metadata.lifecycle_state != "active"
                or content.metadata.protection_context.encrypted
                or content.metadata.protection_context.protection_status == "protected"
            ):
                raise ProtectionDeniedError(
                    "Derivation requires a supported, consistently bound protected operation."
                )
        edges = tuple(
            dict.fromkeys(
                edge
                for content, _, kind in inputs
                for edge in (
                    *content.metadata.provenance.edges,
                    *(
                        LineageEdge(source=source, kind=kind)
                        for source in content.metadata.provenance.sources
                    ),
                )
            )
        )
        lineage = Lineage(edges=edges)
        merged = contexts[0].merge(
            *contexts[1:],
            lineage=lineage,
            policy=ConservativeProtectionMerge(operation, clock=self.clock),
        )
        permits = tuple(
            self.policy.permit(content.metadata.protection_context, binding) for content, binding, _ in inputs
        )
        values = tuple(
            content.release(permit, binding, now=self.clock())
            for (content, binding, _), permit in zip(inputs, permits, strict=True)
        )
        for content, _, _ in inputs:
            self._check_content(content)
        value = _invoke(transform, values)
        now = self.clock()
        merged.require_known(now)
        expiry = min(
            [
                now + 30,
                *(
                    item.metadata.reference.reference.expires_at
                    for item, _, _ in inputs
                    if isinstance(item.metadata.reference.reference, TransientContentHandle)
                ),
                *(value for value in (merged.expires_at, merged.revalidate_at) if value is not None),
            ]
        )
        if expiry <= now:
            raise ProtectionDeniedError("Derived content outlived its input lifetime.")
        # The derived buffer is transient; persistence and every later egress require a new decision.
        metadata = ProtectedContentMetadata(
            reference=ProtectedContentRef(
                reference=TransientContentHandle(
                    handle_id=uuid4().hex,
                    process_instance_id=self.process_instance_id,
                    expires_at=expiry,
                ),
                protection_context_ref=merged.id,
                content_digest=content_digest(value),
                sources=merged.sources,
            ),
            protection_context=merged,
            provenance=lineage,
            authorization_snapshot_refs=tuple(
                dict.fromkeys(
                    ref for item, _, _ in inputs for ref in item.metadata.authorization_snapshot_refs
                )
            ),
            created_at=now,
            lifecycle_state="active",
        )
        return ProtectedContent(value, metadata)
