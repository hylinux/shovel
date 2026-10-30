from __future__ import annotations

from enum import Enum, auto

from pydantic import JsonValue

from shovel.capability.domain import Condition


class _Missing(Enum):
    VALUE = auto()


def lookup(data: JsonValue, path: tuple[str, ...]) -> JsonValue | _Missing:
    current: JsonValue = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return _Missing.VALUE
        current = current[key]
    return current


class ConditionEvaluator:
    def matches(self, condition: Condition, data: JsonValue) -> bool:
        actual = lookup(data, condition.path)
        expected = condition.value
        operator = condition.operator
        if operator == "exists":
            return actual is not _Missing.VALUE
        if actual is _Missing.VALUE:
            return False
        if operator in {"eq", "ne"}:
            equal = type(actual) is type(expected) and actual == expected
            return equal if operator == "eq" else not equal
        if operator == "contains":
            return (
                (isinstance(actual, str) and isinstance(expected, str) and expected in actual)
                or (
                    isinstance(actual, list)
                    and any(type(item) is type(expected) and item == expected for item in actual)
                )
                or (isinstance(actual, dict) and isinstance(expected, str) and expected in actual)
            )
        if operator == "in":
            return isinstance(expected, list) and any(
                type(item) is type(actual) and item == actual for item in expected
            )
        if (
            not isinstance(actual, int | float)
            or isinstance(actual, bool)
            or not isinstance(expected, int | float)
            or isinstance(expected, bool)
        ):
            return False
        if operator == "gt":
            return actual > expected
        if operator == "gte":
            return actual >= expected
        if operator == "lt":
            return actual < expected
        return actual <= expected


def required_value(data: JsonValue, path: tuple[str, ...]) -> JsonValue:
    value = lookup(data, path)
    if isinstance(value, _Missing):
        raise ValueError("Input mapping path is missing from the event.")
    return value
