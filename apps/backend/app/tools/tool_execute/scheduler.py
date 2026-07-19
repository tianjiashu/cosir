"""Tool scheduler."""

import logging
from typing import Iterable, List

from app.tools.tool_registry import ToolRegistry
from app.tools.tool_execute.executor import ToolExecutor
from app.tools.tool_execute.results import tool_error
from app.tools.schemas import ToolCall, ToolDefinition, ToolObservation
from app.tools.validation.arguments import validate_tool_arguments


class ToolScheduler:
    """Validate policy and execute model-requested tool calls."""

    def __init__(
        self,
        registry: ToolRegistry,
        allowed_permissions: Iterable[str],
        logger: logging.Logger,
        executor: ToolExecutor | None = None,
    ) -> None:
        """Initialize the scheduler."""

        self._registry = registry
        self._allowed_permissions = set(allowed_permissions)
        self._logger = logger
        self._executor = executor or ToolExecutor(logger)

    def list_model_visible_tools(self) -> List[ToolDefinition]:
        """Return tools visible to the model under the scheduler policy."""

        return [
            tool
            for tool in self._registry.get_all_definitions()
            if tool.visible_by_default and tool.permission in self._allowed_permissions
        ]

    def execute(self, call: ToolCall) -> ToolObservation:
        """Execute one tool call and return a normalized observation."""

        tool = self._registry.get_tool_definition(call.tool_name)
        if tool is None:
            return tool_error(
                call.tool_name,
                f"unknown tool: {call.tool_name}",
                reason="unknown_tool",
                tool_call_id=call.call_id,
            )
        if tool.permission not in self._allowed_permissions:
            return tool_error(
                tool.name,
                f"permission denied for tool: {tool.name}",
                reason="permission_denied",
                permission=tool.permission,
                tool_call_id=call.call_id,
            )

        validation = validate_tool_arguments(
            call.arguments,
            tool.parameters_schema,
            tool.args_model,
            tool.required_params,
        )
        if not validation.ok:
            return tool_error(
                tool.name,
                f"invalid tool arguments: {validation.error}",
                reason="invalid_arguments",
                permission=tool.permission,
                tool_call_id=call.call_id,
            )
        return self._executor.execute(tool, validation.arguments, tool_call_id=call.call_id)
