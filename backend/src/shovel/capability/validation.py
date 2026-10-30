from __future__ import annotations

from collections.abc import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import JsonValue

from shovel.capability.domain import (
    CapabilityContent,
    CapabilityKind,
    FunctionContract,
    FunctionImplementation,
    WorkflowImplementation,
)
from shovel.workflow.validation import workflow_errors


def _schema_errors(schema: dict[str, JsonValue]) -> list[str]:
    errors: list[str] = []
    pending: list[tuple[JsonValue, int]] = [(schema, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            return ["Schema exceeds the nesting limit."]
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    errors.append("Only local schema references are allowed.")
                if key == "$schema" and child != "https://json-schema.org/draft/2020-12/schema":
                    errors.append("Only JSON Schema draft 2020-12 is supported.")
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        errors.append("Invalid JSON Schema.")
    return errors


def content_errors(
    kind: CapabilityKind,
    content: CapabilityContent,
    functions: Mapping[str, FunctionContract],
) -> tuple[str, ...]:
    errors: list[str] = []
    if content.implementation.kind != kind.value:
        errors.append("Implementation kind does not match the definition.")
    for label, schema in (("Input", content.input_schema), ("Output", content.output_schema)):
        errors.extend(f"{label}: {error}" for error in _schema_errors(schema))
    allowed = {(item.capability_id, item.version) for item in content.allowed_capabilities}
    locked = {(item.capability_id, item.version) for item in content.dependency_lock.dependencies}
    if len(allowed) != len(content.allowed_capabilities):
        errors.append("Allowed capabilities contain duplicate exact versions.")
    if not allowed.issubset(locked):
        errors.append("Allowed capabilities must be covered by the dependency lock.")
    if not set(content.required_permissions).issubset(content.permission_boundary):
        errors.append("Required permissions must be explicitly included in the permission boundary.")
    if len({item.name for item in content.connection_requirements}) != len(content.connection_requirements):
        errors.append("Connection requirement names must be unique.")
    implementation = content.implementation
    if isinstance(implementation, FunctionImplementation):
        contract = functions.get(implementation.implementation_id)
        if contract is None:
            errors.append("Function implementation is not registered by the platform.")
        else:
            for field in FunctionContract.model_fields:
                if field not in {"implementation_id", "retry_safe"} and getattr(content, field) != getattr(
                    contract, field
                ):
                    errors.append(f"Function {field} does not match its trusted contract.")
    if isinstance(implementation, WorkflowImplementation):
        errors.extend(workflow_errors(content))
        for node in implementation.nodes:
            errors.extend(f"Node response: {error}" for error in _schema_errors(node.response_schema))
    return tuple(errors)
