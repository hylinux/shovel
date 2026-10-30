from __future__ import annotations

from collections.abc import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import JsonValue
from referencing import Registry
from referencing.exceptions import Unresolvable

from shovel.capability.domain import CapabilityContent, InputBinding, WorkflowEdge, WorkflowImplementation


def _path_schema(schema: dict[str, JsonValue], path: tuple[str, ...]) -> dict[str, JsonValue] | None:
    current = schema
    for key in path:
        properties = current.get("properties")
        value = properties.get(key) if isinstance(properties, dict) else None
        if not isinstance(value, dict):
            return None
        current = value
    return current


def workflow_errors(
    content: CapabilityContent,
    dependencies: Mapping[tuple[str, int], CapabilityContent] | None = None,
) -> tuple[str, ...]:
    graph = content.implementation
    if not isinstance(graph, WorkflowImplementation):
        return ()
    errors: list[str] = []
    nodes = {node.id: node for node in graph.nodes}
    if len(nodes) != len(graph.nodes):
        errors.append("Workflow node IDs must be unique.")
    starts = [node.id for node in graph.nodes if node.kind == "input"]
    ends = [node.id for node in graph.nodes if node.kind == "output"]
    if len(starts) != 1 or len(ends) != 1:
        return (*errors, "Workflow requires exactly one input and one output node.")
    outgoing: dict[str, list[WorkflowEdge]] = {identifier: [] for identifier in nodes}
    incoming: dict[str, set[str]] = {identifier: set() for identifier in nodes}
    for edge in graph.edges:
        if edge.source not in nodes or edge.target not in nodes:
            errors.append("Workflow edge references an unknown node.")
        else:
            outgoing[edge.source].append(edge)
            incoming[edge.target].add(edge.source)
    if errors:
        return tuple(errors)
    if incoming[starts[0]]:
        errors.append("Workflow input cannot have incoming edges.")
    order: list[str] = []
    pending = {key: set(value) for key, value in incoming.items()}
    ready = [key for key, value in pending.items() if not value]
    while ready:
        identifier = ready.pop()
        order.append(identifier)
        for edge in outgoing[identifier]:
            pending[edge.target].discard(identifier)
            if not pending[edge.target] and edge.target not in order and edge.target not in ready:
                ready.append(edge.target)
    if len(order) != len(nodes):
        return (*errors, "Workflow cycles are not allowed.")
    reachable = {starts[0]}
    dominators: dict[str, set[str]] = {}
    for identifier in order:
        predecessors = incoming[identifier]
        if predecessors:
            dominators[identifier] = set.intersection(*(dominators[parent] for parent in predecessors)) | {
                identifier
            }
        else:
            dominators[identifier] = {identifier}
        if identifier in reachable:
            reachable.update(edge.target for edge in outgoing[identifier])
    if reachable != set(nodes):
        errors.append("Workflow contains unreachable nodes.")
    allowed = set(content.allowed_capabilities)
    for node in graph.nodes:
        labels = [edge.label for edge in outgoing[node.id]]
        expected_labels = {"true", "false"} if node.kind == "condition" else {"next"}
        if node.kind == "output":
            expected_labels = set()
        if node.kind in {"function", "skill"}:
            valid_labels = set(labels) in ({"next"}, {"next", "error"})
        else:
            valid_labels = set(labels) == expected_labels
        if not valid_labels or len(labels) != len(set(labels)):
            errors.append(f"Workflow node {node.id} has invalid outgoing edges.")
        if node.kind in {"function", "skill"}:
            if node.capability is None or node.capability not in allowed:
                errors.append("Function and skill nodes require a declared exact capability.")
        elif node.capability is not None or node.permission_boundary is not None:
            errors.append("Only capability nodes can declare child references or boundaries.")
        if (node.kind == "condition") != (node.condition is not None):
            errors.append("Condition nodes require a declarative condition, other nodes must not have one.")
        if node.kind == "ask_user" and not node.prompt.strip():
            errors.append("Ask User requires a non-blank prompt.")
        if (node.kind == "emit_event") != (node.event_type is not None):
            errors.append("Only Emit Event nodes require an event type.")
        if node.input_mapping and node.kind not in {
            "function",
            "skill",
            "notification",
            "emit_event",
            "output",
        }:
            errors.append("This node kind does not support input mapping.")
    if dependencies is None or errors:
        return tuple(errors)
    for schema in (
        content.input_schema,
        content.output_schema,
        *(node.response_schema for node in graph.nodes),
        *(child.input_schema for child in dependencies.values()),
        *(child.output_schema for child in dependencies.values()),
    ):
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError:
            return ("Workflow requires valid JSON Schemas.",)
    schemas: dict[str, dict[str, JsonValue]] = {}
    for node in graph.nodes:
        child = (
            dependencies.get((node.capability.capability_id, node.capability.version))
            if node.capability
            else None
        )
        if node.kind in {"function", "skill"}:
            if child is None or child.implementation.kind != node.kind:
                errors.append("Workflow capability node kind or dependency does not match.")
                continue
            schemas[node.id] = child.output_schema
            boundary = (
                node.permission_boundary
                if node.permission_boundary is not None
                else content.permission_boundary
            )
            if not set(boundary).issubset(content.permission_boundary) or not set(
                child.required_permissions
            ).issubset(boundary):
                errors.append(
                    "Workflow child permission boundary cannot expand or omit required permissions."
                )
        elif node.kind == "input":
            schemas[node.id] = content.input_schema
        elif node.kind == "ask_user":
            schemas[node.id] = node.response_schema
        elif node.kind == "condition":
            schemas[node.id] = {"type": "object", "properties": {"result": {"type": "boolean"}}}
        elif node.kind == "approval":
            schemas[node.id] = {"type": "object", "properties": {"approved": {"type": "boolean"}}}
        else:
            key = "notification_id" if node.kind == "notification" else "event_id"
            schemas[node.id] = {"type": "object", "properties": {key: {"type": "string"}}}

    def source(binding: InputBinding, node_id: str) -> dict[str, JsonValue] | None:
        if binding.source != "context":
            return None
        path = binding.path
        if path[0] == "input":
            return _path_schema(content.input_schema, path[1:])
        if len(path) >= 2 and path[0] == "nodes" and path[1] in dominators[node_id] - {node_id}:
            return _path_schema(schemas.get(path[1], {}), path[2:])
        return None

    for node in graph.nodes:
        target: dict[str, JsonValue] = {}
        if node.kind == "output":
            target = content.output_schema
            if target.get("type", "object") != "object":
                errors.append("Workflow output must be an object.")
        elif node.capability is not None:
            child = dependencies.get((node.capability.capability_id, node.capability.version))
            if child is not None:
                target = child.input_schema
                if target.get("type", "object") != "object":
                    errors.append("Workflow node input must be an object.")
        required = target.get("required", [])
        if isinstance(required, list) and not set(required).issubset(node.input_mapping):
            errors.append("Workflow mapping omits required input or output fields.")
        properties = target.get("properties", {})
        for key, binding in node.input_mapping.items():
            expected = properties.get(key, {}) if isinstance(properties, dict) else {}
            if expected is False:
                errors.append("Workflow mapping targets a forbidden property.")
                continue
            if expected is True:
                expected = {}
            if not isinstance(expected, dict):
                continue
            if target.get("additionalProperties") is False and (
                not isinstance(properties, dict) or key not in properties
            ):
                errors.append("Workflow mapping produces an unknown field.")
            if binding.source == "literal":
                try:
                    valid = Draft202012Validator(expected, registry=Registry()).is_valid(binding.value)
                except Unresolvable:
                    errors.append("Workflow mapping references an unsupported schema fragment.")
                    continue
                if not valid:
                    errors.append("Workflow literal mapping violates its target schema.")
            else:
                actual = source(binding, node.id)
                if actual is None:
                    errors.append("Workflow mapping references an invalid or unavailable path.")
                else:
                    for constraint, value in expected.items():
                        if constraint in {"title", "description", "default", "examples", "$schema"}:
                            continue
                        if constraint == "type" and actual.get("type") == "integer" and value == "number":
                            continue
                        if actual.get(constraint) != value:
                            errors.append("Workflow mapping schema compatibility cannot be established.")
                            break
        if node.condition is not None:
            condition = node.condition
            actual = source(InputBinding(source="context", path=condition.path), node.id)
            if actual is None:
                errors.append("Workflow condition references an invalid or unavailable path.")
            elif condition.operator in {"gt", "gte", "lt", "lte"} and (
                actual.get("type") not in {"integer", "number"}
                or isinstance(condition.value, bool)
                or not isinstance(condition.value, int | float)
            ):
                errors.append("Workflow ordered conditions require numbers.")
            elif condition.operator == "in" and not isinstance(condition.value, list):
                errors.append("Workflow in condition requires a list.")
    levels = ("low", "medium", "high", "critical")
    effects = ("none", "local_write", "external_write", "destructive")
    maximum_risk = maximum_effect = 0
    for child in dependencies.values():
        if not set(child.required_permissions).issubset(content.required_permissions):
            errors.append("Workflow risk/permission summary omits a dependency permission.")
        maximum_risk = max(
            maximum_risk,
            levels.index(child.risk_profile.level),
            effects.index(child.side_effect_profile.kind),
        )
        maximum_effect = max(maximum_effect, effects.index(child.side_effect_profile.kind))
    if any(node.kind in {"notification", "emit_event"} for node in graph.nodes):
        maximum_risk, maximum_effect = max(maximum_risk, 1), max(maximum_effect, 1)
    if (
        levels.index(content.risk_profile.level) < maximum_risk
        or effects.index(content.side_effect_profile.kind) < maximum_effect
    ):
        errors.append("Workflow risk or side-effect summary understates its nodes.")
    return tuple(errors)
