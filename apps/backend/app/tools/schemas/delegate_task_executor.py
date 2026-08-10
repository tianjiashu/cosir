"""delegate_task runtime execution contract."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.tools.schemas.tool_execution_context import ToolExecutionContext
    from app.tools.schemas.tool_observation import ToolObservation
    from app.tools.tool_models.delegate_task_args import DelegateTaskArgs


class DelegateTaskExecutor(Protocol):
    """Execute a validated delegate_task request through the runtime."""

    def execute(
        self,
        args: DelegateTaskArgs,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """Execute one child-agent delegation request.

        Args:
            args: Validated delegate_task request parameters.
            execution_context: Parent tool execution boundary and runtime dependencies.

        Returns:
            The child delegation result as a normalized tool observation.

        Raises:
            Implementation-defined exceptions when runtime delegation cannot complete.

        Side Effects:
            Implementations may create and run a child-agent delegation.
        """
        ...
