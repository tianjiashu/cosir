"""Model tool for FIFO Child Agent mailbox messages."""

from __future__ import annotations

import json
from typing import ClassVar

from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_models.child_task import ChildAgentSendArgs
from app.service.child_agent.child_agent_session_error import ChildAgentSessionError


class ChildAgentSendTool:
    """Validate ownership through the injected Child Agent session service."""

    name: ClassVar[str] = "child_agent_send"
    permission: ClassVar[str] = "child_agent_send"
    description: ClassVar[str] = "Send a follow-up message to a directly owned Child Agent."

    def execute(
        self,
        child_task_id: int,
        message: str,
        message_id: str,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """Enqueue a message without mutating the child context from the parent thread."""

        if execution_context is None:
            return tool_error(
                self.name,
                "child_agent_send requires an execution context.",
                permission=self.permission,
            )
        service = execution_context.runtime_dependencies.child_agent_session_service
        if service is None:
            return tool_error(
                self.name, "child_agent_send is not configured.", permission=self.permission
            )
        try:
            result = service.send(
                parent_task_id=execution_context.task_id,
                parent_run_id=execution_context.run_id,
                child_task_id=ChildAgentSendArgs(
                    child_task_id=child_task_id, message=message, message_id=message_id
                ).child_task_id,
                message=message,
                message_id=message_id,
            )
        except ChildAgentSessionError as exc:
            return tool_error(
                self.name,
                exc.code,
                reason=f"Child Agent send rejected: {exc.code}.",
                permission=self.permission,
            )
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=json.dumps(
                {"accepted_seq": result.accepted_seq, "duplicate": result.duplicate}
            ),
        )

    def to_definition(self) -> ToolDefinition:
        """Return the synchronous tool definition."""

        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=ChildAgentSendArgs,
            handler_kind="sync",
            display=ToolDisplayHints(
                verb="发送给子 Agent",
                icon="send",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=True,
            ),
        )


def build_child_agent_send_definition() -> ToolDefinition:
    """Build the Child Agent send tool definition."""

    return ChildAgentSendTool().to_definition()
