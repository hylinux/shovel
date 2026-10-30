from __future__ import annotations

from shovel.capability.domain import CapabilityReference, CapabilityScope
from shovel.capability.registry import CapabilityRegistry
from shovel.domain.identity import new_id
from shovel.execution.domain import (
    CapabilitySnapshot,
    ChildInvocation,
    Execution,
    ExecutionLease,
    ExecutionSnapshot,
    Invocation,
)
from shovel.execution.ports import ExecutionRepository
from shovel.execution.validation import validate_data
from shovel.runtime.errors import RuntimeFailure
from shovel.runtime.requests import request_fingerprint
from shovel.security.services import SecurityGate


def _fingerprint(invocation: Invocation | ChildInvocation) -> str:
    data = invocation.model_dump(mode="json")
    if isinstance(invocation, Invocation):
        context = data["context"]
        for field in (
            "proactive_boundary",
            "workflow_boundary",
            "skill_boundary",
            "proactive_preference",
            "proactive_snapshot",
        ):
            if context[field] is None:
                context.pop(field)
        if not context["workflow_approval_required"]:
            context.pop("workflow_approval_required")
        kind = "capability.invoke"
    else:
        if data["permission_ceiling"] is None:
            data.pop("permission_ceiling")
        if not data["workflow_approval_required"]:
            data.pop("workflow_approval_required")
        kind = "capability.child"
    return request_fingerprint(kind, data)


class InvocationService:
    def __init__(
        self,
        registry: CapabilityRegistry,
        repository: ExecutionRepository,
        *,
        security: SecurityGate | None = None,
    ) -> None:
        self.registry, self.repository = registry, repository
        self.security = security or SecurityGate()

    async def find_request(self, scope: CapabilityScope, key: str) -> Execution | None:
        return await self.repository.find_request(scope, key)

    async def _resolve(
        self, invocation: Invocation | ChildInvocation, parent: ExecutionLease | None
    ) -> CapabilitySnapshot:
        if parent is None:
            assert isinstance(invocation, Invocation)
            scope = invocation.context.scope
        else:
            scope = parent.execution.snapshot.context.scope
        version = await self.registry.resolve_exact(
            scope,
            invocation.capability.capability_id,
            invocation.capability.version,
        )
        definition = await self.registry.get(scope, version.capability_id)
        if version.content_hash is None:
            raise RuntimeFailure("Capability is not published.", status=409)
        return CapabilitySnapshot(
            capability_id=version.capability_id,
            version_id=version.id,
            number=version.number,
            kind=definition.kind,
            content_hash=version.content_hash,
            content=version.content,
        )

    async def invoke(self, invocation: Invocation) -> Execution:
        invocation = Invocation.model_validate(invocation.model_dump(mode="json"))
        fingerprint = _fingerprint(invocation)
        previous = await self.repository.replay(
            invocation.context.scope,
            fingerprint,
            request_key=invocation.request_key,
        )
        if previous is not None:
            return previous
        capability = await self._resolve(invocation, None)
        context = invocation.context.model_copy(
            update={
                "correlation_id": invocation.context.correlation_id or new_id("corr"),
            }
        )
        granted, _, _, _ = await self.security.prepare(capability, context)
        validate_data(capability.content.input_schema, invocation.input)
        snapshot = ExecutionSnapshot(
            capability=capability,
            context=context,
            budget=invocation.budget,
            capability_call_path=(invocation.capability,),
            granted_permissions=granted,
        )
        return await self.repository.create(
            snapshot,
            dict(invocation.input),
            fingerprint,
            request_key=invocation.request_key,
        )

    async def invoke_child(self, parent: ExecutionLease, invocation: ChildInvocation) -> Execution:
        invocation = ChildInvocation.model_validate(invocation.model_dump(mode="json"))
        scope = parent.execution.snapshot.context.scope
        fingerprint = _fingerprint(invocation)
        previous = await self.repository.replay(
            scope,
            fingerprint,
            parent_id=parent.execution.id,
            child_key=invocation.call_key,
        )
        if previous is not None:
            return previous
        current = await self.repository.get(scope, parent.execution.id)
        reference = invocation.capability
        if any(
            item.capability_id == reference.capability_id for item in current.snapshot.capability_call_path
        ):
            raise RuntimeFailure("Recursive capability invocation is forbidden.", status=409)
        allowed = current.snapshot.capability.content.allowed_capabilities
        if reference not in allowed:
            raise RuntimeFailure("Child capability is not declared by the frozen parent version.", status=403)
        capability = await self._resolve(invocation, parent)
        lock = {
            (item.capability_id, item.version): item.content_hash
            for item in current.snapshot.capability.content.dependency_lock.dependencies
        }
        if lock.get((capability.capability_id, capability.number)) != capability.content_hash:
            raise RuntimeFailure("Child capability does not match the frozen dependency lock.", status=409)
        budget = invocation.budget or current.snapshot.budget
        if not budget.narrows(current.snapshot.budget):
            raise RuntimeFailure("Child execution cannot enlarge its parent's budget.", status=403)
        parent_permissions = (await self.security.evaluate(current)).permissions
        ceiling = invocation.permission_ceiling
        if ceiling is not None and not set(ceiling).issubset(parent_permissions):
            raise RuntimeFailure("Child execution cannot enlarge its parent's permissions.", status=403)
        context = current.snapshot.context.model_copy(
            update={
                "source": "child",
                "permission_ceiling": ceiling
                if ceiling is not None
                else current.snapshot.context.permission_ceiling,
                "workflow_approval_required": (
                    current.snapshot.context.workflow_approval_required
                    or invocation.workflow_approval_required
                ),
            }
        )
        granted, _, _, _ = await self.security.prepare(
            capability,
            context,
            parent_permissions=parent_permissions,
        )
        validate_data(capability.content.input_schema, invocation.input)
        path: tuple[CapabilityReference, ...] = (*current.snapshot.capability_call_path, reference)
        if len(path) > budget.max_call_depth:
            raise RuntimeFailure("Execution call depth exceeded.", status=409)
        return await self.repository.create(
            ExecutionSnapshot(
                capability=capability,
                context=context,
                budget=budget,
                capability_call_path=path,
                granted_permissions=granted,
            ),
            dict(invocation.input),
            fingerprint,
            parent=parent,
            child_key=invocation.call_key,
        )
