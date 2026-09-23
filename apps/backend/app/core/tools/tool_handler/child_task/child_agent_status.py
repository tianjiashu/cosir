"""Model tool for canonical Child Agent status reads."""

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
from app.core.tools.tool_models.child_task import ChildAgentStatusArgs
from app.service.child_agent.child_agent_session_error import ChildAgentSessionError


class ChildAgentStatusTool:
    """Read status only from the canonical child Run record."""

    name: ClassVar[str] = "child_agent_status"
    permission: ClassVar[str] = "child_agent_status"
    description: ClassVar[str] = "Read the canonical status and final output of a Child Agent."

    def execute(
        self, child_task_id: int, execution_context: ToolExecutionContext | None = None
    ) -> ToolObservation:
        """Return a safe status projection for a directly owned child."""

        if execution_context is None:
            return tool_error(
                self.name,
                "child_agent_status requires an execution context.",
                permission=self.permission,
            )
        service = execution_context.runtime_dependencies.child_agent_session_service
        if service is None:
            return tool_error(
                self.name, "child_agent_status is not configured.", permission=self.permission
            )
        try:
            args = ChildAgentStatusArgs(child_task_id=child_task_id)
            result = service.status(
                parent_task_id=execution_context.task_id,
                parent_run_id=execution_context.run_id,
                child_task_id=args.child_task_id,
            )
        except ChildAgentSessionError as exc:
            return tool_error(
                self.name,
                exc.code,
                reason=f"Child Agent status rejected: {exc.code}.",
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
            args_model=ChildAgentStatusArgs,
            handler_kind="sync",
            display=ToolDisplayHints(
                verb="读取子 Agent 状态",
                icon="info",
                surface="standalone",
                expandable=False,
                expand_layout="details",
                show_result=True,
            ),
        )


def build_child_agent_status_definition() -> ToolDefinition:
    """Build the Child Agent status tool definition."""

    return ChildAgentStatusTool().to_definition()
