from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agent_framework import Agent, AgentSession, SupportsChatGetResponse

from shovel.capability.domain import AgentImplementation, FunctionImplementation, SkillImplementation
from shovel.execution.domain import BudgetUsage, ExecutorResult
from shovel.execution.ports import ExecutorContext
from shovel.runtime.errors import RuntimeFailure
from shovel.workflow.executor import WorkflowExecutor as WorkflowExecutor

type FunctionHandler = Callable[[ExecutorContext], Awaitable[ExecutorResult]]


class FunctionExecutor:
    def __init__(self, functions: Mapping[str, FunctionHandler]) -> None:
        self._functions = dict(functions)

    async def execute(self, context: ExecutorContext) -> ExecutorResult:
        implementation = context.execution.snapshot.capability.content.implementation
        if not isinstance(implementation, FunctionImplementation):
            raise RuntimeFailure("Function executor received another capability kind.")
        handler = self._functions.get(implementation.implementation_id)
        if handler is None:
            raise RuntimeFailure("Function handler is not installed by the platform.", status=503)
        return await handler(context)


class SkillExecutor:
    def __init__(self, client: Callable[[], SupportsChatGetResponse[Any]] | None = None) -> None:
        self._client = client

    async def execute(self, context: ExecutorContext) -> ExecutorResult:
        implementation = context.execution.snapshot.capability.content.implementation
        if self._client is None or not isinstance(implementation, SkillImplementation):
            raise RuntimeFailure("Skill model adapter is not configured.", status=501)
        await context.consume(BudgetUsage(agent_turns=1))
        agent = Agent(
            client=self._client(),
            name="capability_skill",
            instructions=implementation.instructions,
            tools=[],
        )
        response = await agent.run(
            json.dumps(context.execution.input, ensure_ascii=False),
            session=AgentSession(session_id=context.execution.id),
        )
        return ExecutorResult(output={"text": response.text})


class AgentExecutor:
    def __init__(self, client: Callable[[], SupportsChatGetResponse[Any]]) -> None:
        self._client = client

    async def execute(self, context: ExecutorContext) -> ExecutorResult:
        implementation = context.execution.snapshot.capability.content.implementation
        if not isinstance(implementation, AgentImplementation):
            raise RuntimeFailure("Agent executor received another capability kind.")
        await context.consume(BudgetUsage(agent_turns=1))
        # A fresh, tool-free Agent cannot inherit mutable workspace extensions or parent privileges.
        agent = Agent(
            client=self._client(),
            name="capability_agent",
            instructions=implementation.instructions,
            tools=[],
        )
        response = await agent.run(
            json.dumps(context.execution.input, ensure_ascii=False),
            session=AgentSession(session_id=context.execution.id),
        )
        return ExecutorResult(output={"text": response.text})
