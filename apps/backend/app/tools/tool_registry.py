"""In-memory registry for tool definitions."""

import logging
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

from app.tools.schemas.tool_definition import ToolDefinition

logger = logging.getLogger("coding_agent.backend")


class ToolRegistry:
    """Register, query, and export project tool definitions."""

    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        """Initialize the registry with optional tool definitions."""

        self._tool_definitions: Dict[str, ToolDefinition] = {}
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

    def get_tool_definition(self, name: str) -> Optional[ToolDefinition]:
        """Return a tool definition by name, or None when missing."""

        with self._lock:
            return self._tool_definitions.get(name)

    def get_schema(self, name: str) -> Optional[Mapping[str, Any]]:
        """Return the registered parameter schema for one tool."""

        tool_definition = self.get_tool_definition(name)
        return tool_definition.parameters_schema if tool_definition else None

    def get_all_tool_names(self) -> List[str]:
        """Return all registered tool names in deterministic order."""

        with self._lock:
            return sorted(self._tool_definitions)

    def get_all_definitions(self) -> List[ToolDefinition]:
        """Return all registered tool definitions in deterministic order."""

        with self._lock:
            return [self._tool_definitions[name] for name in sorted(self._tool_definitions)]

    def get_tools_by_permission(self, permission: str) -> List[ToolDefinition]:
        """Return all tool definitions that use one permission label."""

        with self._lock:
            return [
                definition
                for definition in self._tool_definitions.values()
                if definition.permission == permission
            ]

    def get_permissions(self) -> Set[str]:
        """Return every permission label used by registered tools."""

        with self._lock:
            return {definition.permission for definition in self._tool_definitions.values()}

    def get_openai_definitions(
        self, tool_names: Optional[Set[str]] = None
    ) -> List[Dict[str, Any]]:
        """Return model-facing tool definitions in OpenAI function format."""

        with self._lock:
            definitions = list(self._tool_definitions.values())
        result: List[Dict[str, Any]] = []
        for definition in sorted(definitions, key=lambda item: item.name):
            if tool_names is not None:
                if definition.name not in tool_names:
                    continue
            elif not definition.visible_by_default:
                continue
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": definition.name,
                        "description": definition.description,
                        "parameters": definition.parameters_schema,
                    },
                }
            )
        return result

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
