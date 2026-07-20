"""Tool argument validation."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError


@dataclass(frozen=True)
class ToolArgumentValidation:
    """Validated tool arguments or a readable validation error."""

    ok: bool
    arguments: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def validate_tool_arguments(
    arguments: Any,
    schema: Mapping[str, Any],
    args_model: type[BaseModel] | None = None,
    required_params: tuple[str, ...] = (),
) -> ToolArgumentValidation:
    """Validate model-provided tool arguments."""

    if args_model is not None:
        try:
            model = args_model.model_validate(arguments)
        except PydanticValidationError as exc:
            first_error = exc.errors()[0] if exc.errors() else {}
            message = first_error.get("msg") or str(exc)
            return ToolArgumentValidation(ok=False, error=str(message))
        return ToolArgumentValidation(ok=True, arguments=model.model_dump())

    if not schema:
        if isinstance(arguments, Mapping):
            missing = [param for param in required_params if param not in arguments]
            if missing:
                return ToolArgumentValidation(
                    ok=False, error=f"missing required argument: {missing[0]}"
                )
            return ToolArgumentValidation(ok=True, arguments=dict(arguments))
        return ToolArgumentValidation(ok=False, error="expected object")

    if not isinstance(arguments, Mapping):
        return ToolArgumentValidation(ok=False, error="expected object")

    try:
        Draft202012Validator(schema).validate(dict(arguments))
    except JsonSchemaValidationError as exc:
        return ToolArgumentValidation(ok=False, error=exc.message)
    missing = [param for param in required_params if param not in arguments]
    if missing:
        return ToolArgumentValidation(ok=False, error=f"missing required argument: {missing[0]}")
    return ToolArgumentValidation(ok=True, arguments=dict(arguments))
