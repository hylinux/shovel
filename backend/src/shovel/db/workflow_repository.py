from __future__ import annotations

import time
from collections.abc import Callable

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shovel.capability.domain import CapabilityScope, WorkflowImplementation, WorkflowNode
from shovel.db.approval_repository import SQLiteApprovalRepository
from shovel.db.execution_repository import SQLiteExecutionRepository, _record
from shovel.db.interaction_repository import project
from shovel.db.model.interaction import InputRequest as InputRow
from shovel.db.model.interaction import Notification
from shovel.db.model.runtime import Operation
from shovel.db.unit_of_work import sqlite_write
from shovel.domain.identity import new_id
from shovel.domain.safe_data import ensure_no_credentials
from shovel.execution.domain import ExecutionLease, ExecutionState, bounded_json
from shovel.execution.validation import validate_data
from shovel.runtime.errors import NotFound, RuntimeFailure
from shovel.security.domain import SecurityDenied
from shovel.security.ports import ExecutionGate
from shovel.workflow.domain import InputRequest, WorkflowCheckpoint


def _input(row: InputRow) -> InputRequest:
    return InputRequest(
        id=row.id,
        execution_id=row.execution_id,
        user_id=row.user_id,
        node_id=row.node_id,
        state=row.state,
        response=row.response,
        prompt=row.prompt,
        response_schema=row.response_schema,
        expires_at=row.expires_at,
    )


class SQLiteWorkflowRepository:
    async def check(self, lease: ExecutionLease, gate: ExecutionGate) -> None:
        async with sqlite_write(self.sessions) as session:
            row = await self._leased(session, lease)
            await gate.evaluate(_record(row))
            await session.commit()

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, clock: Callable[[], float] = time.time
    ) -> None:
        self.sessions, self.clock = sessions, clock
        self.executions = SQLiteExecutionRepository(sessions, clock=clock)

    async def _leased(self, session: AsyncSession, lease: ExecutionLease) -> Operation:
        row = await self.executions._find(session, lease.execution.snapshot.context.scope, lease.execution.id)
        self.executions._leased(row, lease)
        if not isinstance(_record(row).snapshot.capability.content.implementation, WorkflowImplementation):
            raise RuntimeFailure("Workflow control requires a workflow execution.")
        return row

    @staticmethod
    def _node(lease: ExecutionLease, node: WorkflowNode) -> None:
        graph = lease.execution.snapshot.capability.content.implementation
        if not isinstance(graph, WorkflowImplementation) or node not in graph.nodes:
            raise RuntimeFailure("Workflow node is not part of the frozen definition.")

    async def save(self, lease: ExecutionLease, checkpoint: WorkflowCheckpoint, gate: ExecutionGate) -> None:
        ensure_no_credentials(checkpoint)
        checkpoint = WorkflowCheckpoint.model_validate(checkpoint.model_dump(mode="json"))
        async with sqlite_write(self.sessions) as session:
            row = await self._leased(session, lease)
            graph = _record(row).snapshot.capability.content.implementation
            assert isinstance(graph, WorkflowImplementation)
            nodes = {node.id: node for node in graph.nodes}
            old = WorkflowCheckpoint.model_validate(row.workflow_data) if row.workflow_data else None
            if checkpoint.node_id not in nodes or not set(checkpoint.values).issubset(nodes):
                raise RuntimeFailure("Invalid workflow checkpoint.")
            if old is None:
                if nodes[checkpoint.node_id].kind != "input":
                    raise RuntimeFailure("Workflow checkpoint must start at input.")
            elif old.node_id != checkpoint.node_id:
                if not any(
                    edge.source == old.node_id and edge.target == checkpoint.node_id for edge in graph.edges
                ):
                    raise RuntimeFailure("Invalid workflow checkpoint transition.")
                if nodes[old.node_id].kind == "approval":
                    approval = await SQLiteApprovalRepository.latest(session, row.id)
                    if (
                        old.wait_reason != "waiting_for_approval"
                        or approval is None
                        or approval.state != "consumed"
                        or approval.consumed_attempt != row.attempts
                    ):
                        raise RuntimeFailure("Workflow approval cannot be skipped.")
                if nodes[old.node_id].kind == "ask_user":
                    request = await session.scalar(
                        select(InputRow).where(
                            InputRow.execution_id == row.id, InputRow.node_id == old.node_id
                        )
                    )
                    if (
                        request is None
                        or request.state != "answered"
                        or checkpoint.values.get(old.node_id) != request.response
                    ):
                        raise RuntimeFailure("Workflow input cannot be skipped.")
                    request.state = "consumed"
            row.workflow_data = checkpoint.model_dump(mode="json")
            row.row_version += 1
            if checkpoint.wait_reason == "waiting_for_approval":
                evaluation = await gate.evaluate(_record(row))
                pending_approval = await SQLiteApprovalRepository.for_dispatch(
                    session, _record(row), evaluation, self.clock()
                )
                if pending_approval is None:
                    raise RuntimeFailure("Workflow approval gate was not required.")
                row.result = {**row.result, "approval_request_id": pending_approval.id}
            await session.commit()

    async def ask(self, lease: ExecutionLease, node: WorkflowNode) -> InputRequest:
        self._node(lease, node)
        if node.kind != "ask_user":
            raise RuntimeFailure("Input request requires an Ask User node.")
        async with sqlite_write(self.sessions) as session:
            row = await self._leased(session, lease)
            request = await session.scalar(
                select(InputRow).where(InputRow.execution_id == row.id, InputRow.node_id == node.id)
            )
            if request is None:
                scope = lease.execution.snapshot.context.scope
                request = InputRow(
                    id=new_id("interaction"),
                    execution_id=row.id,
                    node_id=node.id,
                    workspace_id=scope.workspace_id,
                    user_id=scope.user_id,
                    state="pending",
                    prompt=node.prompt,
                    response_schema=node.response_schema,
                    expires_at=min(self.clock() + 86400, row.execution_deadline or self.clock()),
                )
                session.add(request)
                await session.flush()
            await project(
                session,
                row,
                "input_request",
                request.id,
                "input_request",
                request.prompt,
                ["VIEW", "PROVIDE_INPUT", "DISMISS", "SNOOZE"],
                self.clock(),
                identifier=request.id,
            )
            if request.state == "pending" and self.clock() >= request.expires_at:
                request.state = "expired"
            await session.commit()
            if request.state in {"expired", "consumed"}:
                raise RuntimeFailure("Input request is expired or already consumed.", status=409)
            return _input(request)

    async def notify(self, lease: ExecutionLease, node: WorkflowNode, payload: dict[str, JsonValue]) -> str:
        self._node(lease, node)
        ensure_no_credentials(payload)
        bounded_json(payload)
        if node.kind != "notification":
            raise RuntimeFailure("Notification requires a Notification node.")
        async with sqlite_write(self.sessions) as session:
            row = await self._leased(session, lease)
            notification = await session.scalar(
                select(Notification).where(
                    Notification.execution_id == row.id, Notification.node_id == node.id
                )
            )
            if notification is None:
                scope = lease.execution.snapshot.context.scope
                notification = Notification(
                    id=new_id("notification"),
                    execution_id=row.id,
                    node_id=node.id,
                    workspace_id=scope.workspace_id,
                    user_id=scope.user_id,
                    message=node.prompt,
                    payload=payload,
                )
                session.add(notification)
                await session.flush()
            await session.commit()
            return notification.id

    async def _find_input(
        self,
        session: AsyncSession,
        scope: CapabilityScope,
        execution_id: str,
        interaction_id: str,
        user_id: str,
    ) -> tuple[Operation, InputRow]:
        if user_id != scope.user_id:
            raise SecurityDenied("Input user does not match the execution owner.")
        execution = await self.executions._find(session, scope, execution_id)
        request = await session.scalar(
            select(InputRow).where(
                InputRow.id == interaction_id,
                InputRow.execution_id == execution_id,
                InputRow.workspace_id == scope.workspace_id,
                InputRow.user_id == user_id,
            )
        )
        if request is None:
            raise NotFound("Interaction", interaction_id)
        return execution, request

    async def get_input(
        self, scope: CapabilityScope, execution_id: str, interaction_id: str, user_id: str
    ) -> InputRequest:
        async with sqlite_write(self.sessions) as session:
            _, request = await self._find_input(session, scope, execution_id, interaction_id, user_id)
            if request.state == "pending" and self.clock() >= request.expires_at:
                request.state = "expired"
            await session.commit()
            return _input(request)

    async def respond(
        self,
        scope: CapabilityScope,
        execution_id: str,
        interaction_id: str,
        user_id: str,
        response: JsonValue,
        gate: ExecutionGate,
    ) -> InputRequest:
        ensure_no_credentials(response)
        bounded_json(response)
        async with sqlite_write(self.sessions) as session:
            execution, request = await self._find_input(session, scope, execution_id, interaction_id, user_id)
            if self.clock() >= request.expires_at and request.state == "pending":
                request.state = "expired"
                await session.commit()
            if request.state != "pending":
                raise RuntimeFailure("Input response is expired or cannot be replayed.", status=409)
            checkpoint = WorkflowCheckpoint.model_validate(execution.workflow_data)
            if (
                execution.state != ExecutionState.SUSPENDED
                or checkpoint.node_id != request.node_id
                or _record(execution).wait_reason != "waiting_for_input"
            ):
                raise RuntimeFailure("Execution is not waiting for this input.", status=409)
            await gate.evaluate(_record(execution))
            validate_data(request.response_schema, response)
            request.response, request.state = response, "answered"
            await session.commit()
            return _input(request)
