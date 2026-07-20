"""In-memory registry for tool definitions."""

import logging
import threading
from collections.abc import Iterable, Mapping
from typing import Any

from app.tools.schemas.tool_definition import ToolDefinition

logger = logging.getLogger("coding_agent.backend")


class ToolRegistry:
    """Register, query, and export project tool definitions."""

    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        """Initialize the registry with optional tool definitions."""

        self._tool_definitions: dict[str, ToolDefinition] = {}
        self._lock = threading.RLock()
        self._generation = 0
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        """Register a tool definition by name."""

        if not isinstance(definition, ToolDefinition):
            raise TypeError(f"expected ToolDefinition, got {type(definition).__name__}")
        normalized = definition.normalized()
        with self._lock:
            if normalized.name in self._tool_definitions:
                logger.warning(
                    "tool_already_registered",
                    extra={"data": {"name": normalized.name}},
                )
                return
            self._tool_definitions[normalized.name] = normalized
            self._generation += 1

    def deregister(self, name: str) -> None:
        """Remove a tool definition by name when it exists."""

        with self._lock:
            if name in self._tool_definitions:
                del self._tool_definitions[name]
                self._generation += 1

    def get_tool_definition(self, name: str) -> ToolDefinition | None:
        """Return a tool definition by name, or None when missing."""

        with self._lock:
            return self._tool_definitions.get(name)

    def get_schema(self, name: str) -> Mapping[str, Any] | None:
        """Return the registered parameter schema for one tool."""

        tool_definition = self.get_tool_definition(name)
        return tool_definition.parameters_schema if tool_definition else None

    def get_all_tool_names(self) -> list[str]:
        """Return all registered tool names in deterministic order."""

        with self._lock:
            return sorted(self._tool_definitions)

    def get_all_definitions(self) -> list[ToolDefinition]:
        """Return all registered tool definitions in deterministic order."""

        with self._lock:
            return [self._tool_definitions[name] for name in sorted(self._tool_definitions)]

    def get_tools_by_permission(self, permission: str) -> list[ToolDefinition]:
        """Return all tool definitions that use one permission label."""

        with self._lock:
            return [
                definition
                for definition in self._tool_definitions.values()
                if definition.permission == permission
            ]

    def get_permissions(self) -> set[str]:
        """Return every permission label used by registered tools."""

        with self._lock:
            return {definition.permission for definition in self._tool_definitions.values()}

    def dispatch(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """Execute a registered tool handler by name."""

        definition = self.get_tool_definition(name)
        if definition is None:
            raise KeyError(f"unknown tool: {name}")
        return definition.handler(**arguments)

    @property
    def generation(self) -> int:
        """Return the registry generation counter."""

        return self._generation
