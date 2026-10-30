from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import JsonValue
from referencing import Registry
from referencing.exceptions import Unresolvable

from shovel.runtime.errors import RuntimeFailure


def validate_data(schema: dict[str, JsonValue], value: JsonValue) -> None:
    try:
        Draft202012Validator(schema, registry=Registry()).validate(value)
    except (ValidationError, Unresolvable) as exc:
        raise RuntimeFailure("Execution data does not satisfy the frozen JSON Schema.") from exc
