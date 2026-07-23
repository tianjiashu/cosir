"""Tool definition value object."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class ToolDefinition:
    """Executable tool metadata and handler contract."""

    name: str
    description: str
    permission: str
    required_params: Iterable[str]
    handler: Callable[..., Any]
    parameters_schema: Mapping[str, Any] = field(default_factory=dict)
    args_model: type[BaseModel] | None = None
    timeout_seconds: float = 10.0
    risk_level: str = "low"
    visible_by_default: bool = True
    resource_keys: Sequence[str] = field(default_factory=tuple)

    def normalized(self) -> "ToolDefinition":
        """Return a definition with a derived schema when args_model is provided."""

        if self.parameters_schema or self.args_model is None:
            return self
        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            required_params=self.required_params,
            handler=self.handler,
            parameters_schema=self.args_model.model_json_schema(),
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            visible_by_default=self.visible_by_default,
            resource_keys=self.resource_keys,
        )

    def to_model_tool_definition(self) -> dict[str, Any]:
        """Return a model-facing tool definition."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters_schema),
        }
