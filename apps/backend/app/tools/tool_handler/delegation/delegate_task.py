"""delegate_task tool handler."""

from typing import ClassVar

from pydantic import ValidationError

from app.tools.schemas import ToolDefinition, ToolExecutionContext, ToolObservation
from app.tools.tool_execute.tool_error import tool_error
from app.tools.tool_handler.tool_base import HandlerBase
from app.tools.tool_models.delegate_task_args import DelegateTaskArgs


class DelegateTaskTool(HandlerBase):
    """Validate a delegate_task request and route it to the injected runtime executor."""

    name: str = "delegate_task"
    description: str = "Delegate a focused task to a child agent and return its result."
    permission: ClassVar[str] = "delegate_task"
    args_model: type[DelegateTaskArgs] = DelegateTaskArgs
    timeout_seconds: ClassVar[float] = 300.0
    risk_level: ClassVar[str] = "medium"

    def execute(
        self,
        child_agent_id: str,
        delegation_type: str,
        prompt: str,
        requested_tools: list[str],
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """Delegate a task through the runtime executor attached to the execution context.

        Args:
            child_agent_id: Identifier of the child agent profile to run.
            delegation_type: Requested delegation category.
            prompt: Focused instruction for the child agent.
            requested_tools: Tool names requested for the child agent.
            execution_context: Parent tool execution boundary containing runtime dependencies.

        Returns:
            The injected executor's normalized result, or an error observation when the
            execution context, executor, or arguments are unavailable or invalid.

        Raises:
            None. Validation failures are returned as error observations; executor errors
            remain the executor's responsibility.

        Side Effects:
            Invokes the injected runtime executor when the request is valid.
        """

        if execution_context is None:
            return tool_error(
                self.name,
                "delegate_task requires an execution context.",
                reason="Provide the parent task execution context before delegating work.",
                permission=self.permission,
            )

        try:
            args = DelegateTaskArgs.model_validate(
                {
                    "child_agent_id": child_agent_id,
                    "delegation_type": delegation_type,
                    "prompt": prompt,
                    "requested_tools": requested_tools,
                }
            )
        except ValidationError:
            return tool_error(
                self.name,
                "delegate_task received invalid arguments.",
                reason="Correct the delegate_task arguments, then retry.",
                permission=self.permission,
            )

        executor = execution_context.runtime_dependencies.delegate_task_executor
        if executor is None:
            return tool_error(
                self.name,
                "delegate_task_executor is not configured.",
                reason="Configure a delegate_task runtime executor before delegating work.",
                permission=self.permission,
            )
        return executor.execute(args, execution_context)

    def to_definition(self) -> ToolDefinition:
        """Build the registry definition for the delegate_task tool.

        Args:
            None.

        Returns:
            The delegate_task tool definition using in-process thread execution.

        Raises:
            None.

        Side Effects:
            None.
        """

        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            execution_mode="thread",
        )


def build_delegate_task_definition() -> ToolDefinition:
    """Build the delegate_task tool definition.

    Args:
        None.

    Returns:
        The delegate_task definition ready for registry registration.

    Raises:
        None.

    Side Effects:
        Creates a DelegateTaskTool instance without starting a delegation.
    """

    return DelegateTaskTool().to_definition()
