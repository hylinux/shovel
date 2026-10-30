from __future__ import annotations

import hashlib
import json
import secrets
import time
import traceback
from collections.abc import Callable
from typing import Any

import structlog
from pydantic import BaseModel

from shovel.automation.domain import ManualTrigger, ProactiveService, ScheduleTrigger
from shovel.automation.manager import ProactiveServiceManager
from shovel.automation.schedules import next_occurrence
from shovel.capability.domain import (
    Availability,
    CapabilityContent,
    CapabilityKind,
    CapabilityReference,
    CapabilityScope,
    Permission,
)
from shovel.capability.service import CapabilityService
from shovel.execution.control import ExecutionWakeupService
from shovel.execution.domain import CapabilitySnapshot, Execution, Invocation, InvocationContext
from shovel.execution.engine import ExecutionEngine
from shovel.interaction.service import InteractionService
from shovel.platform.authorization import ExplicitActions, PublishAuthorization
from shovel.platform.domain import PlatformCommand
from shovel.platform.ports import PlatformQueries
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.security.domain import SecurityDenied
from shovel.security.services import ApprovalService
from shovel.workflow.service import WorkflowService

CONFIRMED_ACTIONS = frozenset(
    {
        "capability.publish",
        "proactive.enable",
        "proactive.resume",
        "approval.approve",
        "approval.reject",
        "interaction.input",
        "automation.resume",
    }
)


def _json(value: BaseModel) -> dict[str, Any]:
    return value.model_dump(mode="json")


def _required[T](value: T | None, name: str) -> T:
    if value is None:
        raise RuntimeFailure(f"{name} is required.")
    return value


class PlatformApplication:
    """One scoped application boundary for HTTP, CLI and the Main Agent."""

    def __init__(
        self,
        scope: CapabilityScope,
        engine: ExecutionEngine,
        proactive: ProactiveServiceManager,
        approvals: ApprovalService,
        inputs: WorkflowService,
        interactions: InteractionService,
        wakeups: ExecutionWakeupService,
        queries: PlatformQueries,
        actions: ExplicitActions,
        *,
        agent_permissions: Callable[[], tuple[Permission, ...]],
        wake: Callable[[], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.scope, self.engine, self.proactive = scope, engine, proactive
        self.approvals, self.inputs, self.interactions = approvals, inputs, interactions
        self.wakeups, self.queries, self.actions = wakeups, queries, actions
        self.agent_permissions, self.wake, self.clock = agent_permissions, wake, clock
        self.registry = engine.invocations.registry
        self.capabilities = CapabilityService(self.registry, authorizer=PublishAuthorization(actions))
        self._confirmations: dict[str, tuple[str, float]] = {}

    async def permissions(self, *, human: bool) -> tuple[Permission, ...]:
        policy = await self.engine.invocations.security.authorization.provider.load(self.scope)
        allowed = set(policy.user_grants).intersection(policy.platform_constraints)
        if not human:
            allowed.intersection_update(self.agent_permissions())
        return tuple(sorted(allowed, key=lambda p: (p.action, p.resource)))

    async def _stamp(self, command: PlatformCommand) -> tuple[dict[str, Any], tuple[str, ...]]:
        if command.action == "automation.resume":
            control = await self.proactive.control(self.scope)
            if control.revision != _required(command.expected_revision, "expected_revision"):
                raise RuntimeFailure("Automation control has changed.", status=409)
            services = []
            for service in await self.proactive.list(self.scope):
                if service.enabled and not service.paused:
                    preview, _ = await self._stamp(
                        PlatformCommand(
                            action="proactive.enable",
                            identifier=service.id,
                            expected_revision=service.revision,
                        )
                    )
                    services.append(preview)
            return {"control": _json(control), "services": services}, ()
        identifier = _required(command.identifier, "identifier")
        if command.action == "capability.publish":
            number = _required(command.version, "version")
            revision = _required(command.expected_revision, "expected_revision")
            version = await self.registry.get_version(self.scope, identifier, number)
            if version.revision != revision or version.published_at is not None:
                raise RuntimeFailure("The draft has changed or is already published.", status=409)
            validation = await self.capabilities.validate_draft(self.scope, identifier, number)
            if not validation.valid:
                raise RuntimeFailure("Invalid draft: " + "; ".join(validation.errors))
            await self._check_selection(version.content, human=True)
            return {
                **_json(version),
                "definition": _json(await self.registry.get(self.scope, identifier)),
            }, ("publish", identifier, str(number), version.content.content_hash)
        if command.action in {"proactive.enable", "proactive.resume"}:
            service = await self.proactive.get(self.scope, identifier)
            if service.revision != _required(command.expected_revision, "expected_revision"):
                raise RuntimeFailure("The proactive service has changed.", status=409)
            reference = await self.proactive.resolve_target(self.scope, service.configuration.target)
            version = await self.registry.resolve_exact(
                self.scope, reference.capability_id, reference.version
            )
            snapshot = CapabilitySnapshot(
                capability_id=version.capability_id,
                version_id=version.id,
                number=version.number,
                kind=version.content.implementation.kind,
                content_hash=version.content.content_hash,
                content=version.content,
            )
            _, _, risk, approval = await self.engine.invocations.security.prepare(
                snapshot,
                InvocationContext(
                    scope=self.scope,
                    actor_id=self.scope.user_id,
                    source="proactive",
                    permission_ceiling=service.configuration.permission_boundary,
                    proactive_boundary=service.configuration.permission_boundary,
                    proactive_preference=service.configuration.approval_preference,
                ),
            )
            return {
                "service": _json(service),
                "target": _json(version),
                "risk": _json(risk),
                "approval": _json(approval),
            }, ()
        execution_id = _required(command.execution_id, "execution_id")
        execution = await self.engine.repository.get(self.scope, execution_id)
        if command.action != "approval.reject":
            await self.engine.invocations.security.evaluate(execution)
        if command.action.startswith("approval."):
            request = await self.approvals.get(
                self.scope,
                execution_id,
                identifier,
                user_id=self.scope.user_id,
            )
            if request.state != "pending" or request.expires_at <= self.clock():
                raise RuntimeFailure("Approval request is no longer pending.", status=409)
            action = command.action.split(".")[1]
            return _json(request), (action, execution_id, identifier, self.scope.user_id)
        request_input = await self.inputs.get_input(
            self.scope,
            execution_id,
            identifier,
            user_id=self.scope.user_id,
        )
        if request_input.state != "pending" or request_input.expires_at <= self.clock():
            raise RuntimeFailure("Input request is no longer pending.", status=409)
        return _json(request_input), ("input", execution_id, identifier, self.scope.user_id)

    async def execute(self, command: PlatformCommand, *, human: bool) -> dict[str, Any]:
        try:
            return await self._dispatch(command, human=human)
        except RuntimeFailure, ValueError:
            raise
        except Exception as exc:
            structlog.get_logger("shovel.platform").error(
                "platform.internal_failure",
                action=command.action,
                error_type=type(exc).__name__,
                frames=[
                    {"function": frame.name, "line": frame.lineno}
                    for frame in traceback.extract_tb(exc.__traceback__)
                ],
            )
            raise

    async def _dispatch(self, command: PlatformCommand, *, human: bool) -> dict[str, Any]:
        command = PlatformCommand.model_validate(command.model_dump(mode="json"))
        if command.action in CONFIRMED_ACTIONS:
            if not human:
                return {
                    "requires_confirmation": True,
                    "command": command.model_dump(
                        mode="json",
                        exclude={"prepare", "confirmation_id"},
                    ),
                    "confirmation_channel": "explicit_api_or_cli",
                }
            stamp, intent = await self._stamp(command)
            identity = command.model_dump(mode="json", exclude={"prepare", "confirmation_id"})
            digest = hashlib.sha256(
                json.dumps(
                    [identity, stamp],
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest()
            now = self.clock()
            self._confirmations = {key: value for key, value in self._confirmations.items() if value[1] > now}
            if command.prepare:
                if len(self._confirmations) >= 256:
                    raise RuntimeFailure("Too many pending confirmations.", status=429)
                token = secrets.token_urlsafe(24)
                self._confirmations[token] = digest, now + 300
                return {
                    "confirmation_id": token,
                    "expires_at": now + 300,
                    "action": command.action,
                    "preview": stamp,
                    "intent": identity,
                }
            receipt = self._confirmations.pop(command.confirmation_id or "", None)
            if receipt is None or receipt[0] != digest:
                raise RuntimeFailure(
                    "Confirmation expired, changed, or already used; review again.", status=409
                )
            with self.actions.allow(self.scope, *intent):
                result = await self._execute(command, human=human)
        else:
            if command.prepare or command.confirmation_id:
                raise RuntimeFailure("This action does not use a confirmation ticket.")
            result = await self._execute(command, human=human)
        self.wake()
        return result

    async def _execute(self, command: PlatformCommand, *, human: bool) -> dict[str, Any]:
        action, scope = command.action, self.scope
        identifier = command.identifier
        if action == "capability.options":
            return await self._options(human=human)
        if action.startswith("automation."):
            if action == "automation.show":
                return _json(await self.proactive.control(scope))
            return _json(
                await self.proactive.set_paused(
                    scope,
                    paused=action == "automation.pause",
                    expected_revision=_required(command.expected_revision, "expected_revision"),
                )
            )
        if action in {"capability.list", "capability.search"}:
            return {
                "items": [
                    _json(row) for row in await self.queries.capabilities(scope, command.kind, command.query)
                ]
            }
        if action.startswith("capability.") or action == "execution.run":
            if action != "capability.create":
                definition = await self.registry.get(scope, _required(identifier, "identifier"))
                if command.kind is not None and definition.kind != command.kind:
                    raise NotFound("Capability", identifier or "")
            if action == "capability.show":
                if command.version is None:
                    return _json(definition)
                return _json(await self.registry.get_version(scope, definition.id, command.version))
            if action == "capability.versions":
                return {"items": [_json(row) for row in await self.queries.versions(scope, definition.id)]}
            if action == "capability.create":
                kind = _required(command.kind, "kind")
                if not human and kind not in {CapabilityKind.SKILL, CapabilityKind.WORKFLOW}:
                    raise SecurityDenied("The Main Agent may create only Skill and Workflow drafts.")
                await self._check_selection(_required(command.content, "content"), human=human)
                return _json(
                    await self.capabilities.create_draft(
                        scope,
                        kind,
                        command.name,
                        _required(command.content, "content"),
                        description=command.description,
                    )
                )
            if action == "capability.update":
                content = _required(command.content, "content")
                await self._check_selection(content, human=human)
                return _json(
                    await self.capabilities.update_draft(
                        scope,
                        definition.id,
                        _required(command.version, "version"),
                        content,
                        expected_revision=_required(command.expected_revision, "expected_revision"),
                        name=command.name or None,
                        description=command.description,
                    )
                )
            if action == "capability.fork":
                return _json(
                    await self.capabilities.create_draft_from_version(
                        scope,
                        definition.id,
                        _required(command.version, "version"),
                    )
                )
            if action == "capability.validate":
                version = await self.registry.get_version(
                    scope,
                    definition.id,
                    _required(command.version, "version"),
                )
                await self._check_selection(version.content, human=human)
                validation = await self.capabilities.validate_draft(
                    scope, definition.id, _required(command.version, "version")
                )
                return {"valid": validation.valid, **_json(validation)}
            if action == "capability.publish":
                return _json(
                    await self.capabilities.publish(
                        scope,
                        definition.id,
                        _required(command.version, "version"),
                        expected_revision=_required(command.expected_revision, "expected_revision"),
                    )
                )
            if action == "execution.run":
                ceiling = await self.permissions(human=human)
                if command.permissions is not None:
                    if not set(command.permissions).issubset(ceiling):
                        raise SecurityDenied("Invocation permissions may only narrow the caller's grants.")
                    ceiling = command.permissions
                return _json(
                    await self.engine.invocations.invoke(
                        Invocation(
                            capability=CapabilityReference(
                                capability_id=definition.id, version=_required(command.version, "version")
                            ),
                            context=InvocationContext(
                                scope=scope,
                                actor_id=scope.user_id,
                                source="manual" if human else "agent",
                                permission_ceiling=ceiling,
                            ),
                            input=command.input,
                            budget=command.budget,
                            request_key=_required(command.request_key, "request_key"),
                        )
                    )
                )
        if action == "execution.list":
            return {"items": await self._execution_views(await self.queries.executions(scope))}
        if action == "execution.find":
            key = _required(command.request_key, "request_key")
            found = (
                await self.proactive.find_run(scope, identifier, key)
                if identifier
                else await self.engine.invocations.find_request(scope, key)
            )
            return {"execution": (await self._execution_views([found]))[0] if found else None}
        if action.startswith("execution."):
            identifier = _required(identifier, "identifier")
            if action == "execution.cancel":
                await self.engine.cancel(scope, identifier)
            elif action == "execution.resume":
                execution = await self.engine.repository.get(scope, identifier)
                if not human and not set(execution.snapshot.granted_permissions).issubset(
                    await self.permissions(human=False)
                ):
                    raise SecurityDenied("The Execution exceeds the Main Agent permission boundary.")
                await self.wakeups.schedule_resume(scope, identifier, wake_at=self.clock())
            execution = await self.engine.repository.get(scope, identifier)
            return {
                **(await self._execution_views([execution]))[0],
                "wait_reason": execution.wait_reason,
                "attempt_history": [
                    _json(row) for row in await self.engine.repository.attempts(scope, identifier)
                ],
            }
        if action == "proactive.list":
            return {"items": [await self._service_view(row) for row in await self.proactive.list(scope)]}
        if action == "proactive.create":
            return _json(
                await self.proactive.create_draft(
                    scope,
                    _required(command.configuration, "configuration"),
                    name=command.name,
                    description=command.description,
                )
            )
        if action.startswith("proactive."):
            service = await self.proactive.get(scope, _required(identifier, "identifier"))
            if action == "proactive.update":
                return _json(
                    await self.proactive.update(
                        scope,
                        service.id,
                        _required(command.configuration, "configuration"),
                        enabled=False,
                        paused=False,
                        expected_revision=_required(command.expected_revision, "expected_revision"),
                        name=command.name,
                        description=command.description,
                    )
                )
            if action in {"proactive.enable", "proactive.resume", "proactive.pause", "proactive.disable"}:
                return _json(
                    await self.proactive.update(
                        scope,
                        service.id,
                        service.configuration,
                        enabled=service.enabled
                        if action == "proactive.pause"
                        else action != "proactive.disable",
                        paused=action == "proactive.pause",
                        expected_revision=_required(command.expected_revision, "expected_revision"),
                    )
                )
            if action == "proactive.validate":
                try:
                    reference = await self.proactive.resolve_target(scope, service.configuration.target)
                except RuntimeFailure as exc:
                    return {"valid": False, "errors": [exc.message]}
                return {"valid": True, "errors": [], "target": _json(reference)}
            if action == "proactive.run":
                if not human and not set(service.configuration.permission_boundary).issubset(
                    await self.permissions(human=False)
                ):
                    raise SecurityDenied("The service exceeds the Main Agent permission boundary.")
                return _json(
                    await self.proactive.run_now(
                        scope,
                        service.id,
                        request_key=_required(command.request_key, "request_key"),
                        payload=command.input,
                    )
                )
            if action in {"proactive.history", "proactive.explain"}:
                history = await self.queries.executions(scope, service.id)
                if action == "proactive.history":
                    return {"items": await self._execution_views(history)}
                return {
                    "service": _json(service),
                    "current_admission_reason": await self.proactive.admission_reason(
                        service,
                        None,
                        manual=isinstance(service.configuration.trigger, ManualTrigger),
                    ),
                    "last_execution": _json(history[0]) if history else None,
                    "last_wait_reason": history[0].wait_reason if history else None,
                    "boundary": "Only admitted Executions are durable; skipped events are not persisted.",
                }
            return _json(service)
        if action == "approval.list":
            items = []
            for row in await self.queries.approvals(scope):
                execution = await self.engine.repository.get(scope, row.execution_id)
                content = execution.snapshot.capability.content
                items.append(
                    {
                        **_json(row),
                        "capability": _json(execution.snapshot.capability),
                        "service": execution.snapshot.context.proactive_snapshot,
                        "risk": _json(content.risk_profile),
                        "side_effect": _json(content.side_effect_profile),
                    }
                )
            return {"items": items}
        if action.startswith("approval."):
            identifier, execution_id = (
                _required(identifier, "identifier"),
                _required(command.execution_id, "execution_id"),
            )
            method = {
                "approval.show": self.approvals.get,
                "approval.approve": self.approvals.approve,
                "approval.reject": self.approvals.reject,
            }[action]
            return _json(await method(scope, execution_id, identifier, user_id=scope.user_id))
        if action == "interaction.list":
            await self.interactions.refresh()
            return {"items": [_json(row) for row in await self.interactions.list(scope)]}
        if action == "interaction.show":
            return _json(
                await self.inputs.get_input(
                    scope,
                    _required(command.execution_id, "execution_id"),
                    _required(identifier, "identifier"),
                    user_id=scope.user_id,
                )
            )
        if action == "interaction.input":
            return _json(
                await self.inputs.respond(
                    scope,
                    _required(command.execution_id, "execution_id"),
                    _required(identifier, "identifier"),
                    user_id=scope.user_id,
                    response=command.response,
                )
            )
        raise RuntimeFailure("Unsupported platform action.")

    async def _check_selection(self, content: CapabilityContent, *, human: bool) -> None:
        grants = set(await self.permissions(human=human))
        references = {
            (ref.capability_id, ref.version)
            for ref in (*content.allowed_capabilities, *content.dependency_lock.dependencies)
        }
        for identifier, number in references:
            child = await self.registry.resolve_exact(self.scope, identifier, number)
            if not set(child.content.required_permissions).issubset(grants):
                raise SecurityDenied("A selected dependency exceeds the caller's current permissions.")

    async def _options(self, *, human: bool) -> dict[str, Any]:
        grants = await self.permissions(human=human)
        items = []
        for definition in await self.queries.capabilities(self.scope, None, ""):
            if definition.availability in {Availability.DISABLED, Availability.ARCHIVED}:
                continue
            for version in await self.queries.versions(self.scope, definition.id):
                if version.published_at is None or version.availability in {
                    Availability.DISABLED,
                    Availability.ARCHIVED,
                }:
                    continue
                if not set(version.content.required_permissions).issubset(grants):
                    continue
                try:
                    await self.registry.resolve_exact(self.scope, definition.id, version.number)
                except RuntimeFailure as exc:
                    if exc.status != 409:
                        raise
                    continue
                items.append({"definition": _json(definition), "version": _json(version)})
        return {
            "permissions": [_json(grant) for grant in grants],
            "items": items,
            "memory_access": "disabled",
        }

    async def _execution_views(self, executions: list[Execution]) -> list[dict[str, Any]]:
        timings = await self.queries.timings(self.scope, [row.id for row in executions])
        items = []
        for execution in executions:
            timing = timings[execution.id]
            duration = (
                max(0, (timing.finished_at or self.clock()) - timing.started_at)
                if timing.started_at is not None
                else None
            )
            items.append(
                {
                    **_json(execution),
                    **_json(timing),
                    "wait_reason": execution.wait_reason,
                    "duration_seconds": duration,
                }
            )
        return items

    async def _service_view(self, service: ProactiveService) -> dict[str, Any]:
        trigger = service.configuration.trigger
        control = await self.proactive.control(service.scope)
        next_trigger = (
            next_occurrence(trigger.schedule, self.clock())
            if service.enabled
            and not service.paused
            and not control.paused
            and isinstance(trigger, ScheduleTrigger)
            else None
        )
        definition = await self.registry.get(service.scope, service.configuration.target.capability_id)
        target_error = None
        resolved_version = None
        try:
            resolved_version = (
                await self.proactive.resolve_target(service.scope, service.configuration.target)
            ).version
        except RuntimeFailure as exc:
            target_error = exc.message
        return {
            **_json(service),
            "target_name": definition.name,
            "target_kind": definition.kind.value,
            "resolved_version": resolved_version,
            "target_error": target_error,
            "last_activity": await self.proactive.repository.last_invoked(service),
            "next_trigger": next_trigger,
        }
