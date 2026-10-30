from pydantic import JsonValue

from shovel.capability.domain import WorkflowImplementation
from shovel.domain.conditions import ConditionEvaluator, required_value
from shovel.execution.domain import BudgetUsage, ChildInvocation, ExecutionState, ExecutorResult
from shovel.execution.ports import ExecutorContext
from shovel.execution.validation import validate_data
from shovel.runtime.errors import RuntimeFailure
from shovel.workflow.domain import WorkflowCheckpoint


class WorkflowExecutor:
    async def execute(self, context: ExecutorContext) -> ExecutorResult:
        control = context.workflow
        graph = context.execution.snapshot.capability.content.implementation
        if control is None or not isinstance(graph, WorkflowImplementation):
            raise RuntimeFailure("Workflow control adapter is not configured.", status=501)
        nodes = {node.id: node for node in graph.nodes}
        checkpoint = (
            WorkflowCheckpoint.model_validate(context.execution.checkpoint)
            if context.execution.checkpoint
            else WorkflowCheckpoint(node_id=next(node.id for node in graph.nodes if node.kind == "input"))
        )
        while True:
            node = nodes[checkpoint.node_id]
            if not checkpoint.charged:
                await context.consume(BudgetUsage(steps=1))
                checkpoint = checkpoint.model_copy(update={"charged": True})
                await control.save(checkpoint)
            data: dict[str, JsonValue] = {"input": context.execution.input, "nodes": checkpoint.values}
            payload: dict[str, JsonValue] = {
                key: binding.value if binding.source == "literal" else required_value(data, binding.path)
                for key, binding in node.input_mapping.items()
            }
            label = "next"
            output: JsonValue = {}
            if node.kind == "input":
                output = context.execution.input
            elif node.kind in {"function", "skill"}:
                if node.capability is None:
                    raise RuntimeFailure("Workflow child reference is missing.")
                child = await context.call(
                    ChildInvocation(
                        capability=node.capability,
                        call_key=node.id,
                        input=payload,
                        permission_ceiling=node.permission_boundary,
                    )
                )
                if child.state == ExecutionState.SUSPENDED:
                    await control.save(
                        checkpoint.model_copy(
                            update={
                                "wait_reason": "waiting_for_child",
                                "child_execution_id": child.id,
                            }
                        )
                    )
                    return ExecutorResult(suspended=True)
                if child.state != ExecutionState.SUCCEEDED:
                    if not any(edge.source == node.id and edge.label == "error" for edge in graph.edges):
                        raise RuntimeFailure("Workflow child failed.")
                    label, output = "error", {"error": child.error or "child_failed"}
                else:
                    output = child.output
            elif node.kind == "condition":
                if node.condition is None:
                    raise RuntimeFailure("Workflow condition is missing.")
                result = ConditionEvaluator().matches(node.condition, data)
                label, output = ("true" if result else "false"), {"result": result}
            elif node.kind == "approval":
                if checkpoint.wait_reason != "waiting_for_approval":
                    await control.save(checkpoint.model_copy(update={"wait_reason": "waiting_for_approval"}))
                    return ExecutorResult(suspended=True)
                output = {"approved": True}
            elif node.kind == "ask_user":
                interaction = await control.ask(node)
                if interaction.state == "pending":
                    await control.save(checkpoint.model_copy(update={"wait_reason": "waiting_for_input"}))
                    return ExecutorResult(output={"interaction_id": interaction.id}, suspended=True)
                output = interaction.response
            elif node.kind == "notification":
                output = {"notification_id": await control.notify(node, payload)}
            elif node.kind == "emit_event":
                output = {"event_id": await control.emit(node, payload)}
            elif node.kind == "output":
                validate_data(context.execution.snapshot.capability.content.output_schema, payload)
                return ExecutorResult(output=payload)
            checkpoint = WorkflowCheckpoint(
                node_id=next(
                    edge.target for edge in graph.edges if edge.source == node.id and edge.label == label
                ),
                values={**checkpoint.values, node.id: output},
            )
            await control.save(checkpoint)
