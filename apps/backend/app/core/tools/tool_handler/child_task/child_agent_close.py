"""Model tool for explicit, idempotent Child Agent close."""

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
from app.core.tools.tool_models.child_task import ChildAgentCloseArgs


class ChildAgentCloseTool:
    """Fence mailbox input and converge the canonical child Run."""

    name: ClassVar[str] = "child_agent_close"
    permission: ClassVar[str] = "child_agent_close"
    description: ClassVar[str] = "Close a directly owned Child Agent and cancel its active Run."

    def execute(
        self, child_task_id: int, execution_context: ToolExecutionContext | None = None
    ) -> ToolObservation:
        """Perform an explicit close; repeated closes remain successful and idempotent."""

        if execution_context is None:
            return tool_error(
                self.name,
                "child_agent_close requires an execution context.",
                permission=self.permission,
            )
        service = execution_context.runtime_dependencies.child_agent_session_service
        if service is None:
            return tool_error(
                self.name, "child_agent_close is not configured.", permission=self.permission
            )
        try:
            args = ChildAgentCloseArgs(child_task_id=child_task_id)
            result = service.close(
                parent_task_id=execution_context.task_id,
                parent_run_id=execution_context.run_id,
                child_task_id=args.child_task_id,
            )
        except ChildAgentSessionError as exc:
            return tool_error(
                self.name,
                exc.code,
                reason=f"Child Agent close rejected: {exc.code}.",
                permission=self.permission,
            )
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=json.dumps(result.__dict__, ensure_ascii=False),
        )

    def to_definition(self) -> ToolDefinition:
        """Return the synchronous tool definition."""

        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=ChildAgentCloseArgs,
            handler_kind="sync",
            display=ToolDisplayHints(
                verb="关闭子 Agent",
                icon="stop",
                surface="standalone",
                expandable=False,
                expand_layout="details",
                show_result=True,
            ),
        )


def build_child_agent_close_definition() -> ToolDefinition:
    """Build the Child Agent close tool definition."""

    return ChildAgentCloseTool().to_definition()
