"""AgentTeamNodeStatusTool：Team 节点专用工具，只允许提交 status 和 output。"""

from __future__ import annotations

import json
from typing import ClassVar

from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.schemas.tool_names import TOOL_AGENT_TEAM_NODE_STATUS
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_grouping import TOOL_GROUP_AGENT_TEAM
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import AgentTeamNodeStatusArgs


class AgentTeamNodeStatusTool(HandlerBase):
    """Team 节点专用工具，只允许提交 status 和 output。"""

    name = TOOL_AGENT_TEAM_NODE_STATUS
    description = (
        "Submit the current Agent Team node status and output. Do not choose the next node."
    )
    permission: ClassVar[str] = "agent_team_node_status"
    args_model = AgentTeamNodeStatusArgs
    timeout_seconds: ClassVar[float] = 30.0
    risk_level: ClassVar[str] = "low"
    group = TOOL_GROUP_AGENT_TEAM

    def execute(
        self,
        status: str,
        output: str,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """把当前节点结果交给 coordinator，并返回引擎选择的转移结果。"""

        if execution_context is None:
            return tool_error(
                self.name,
                "agent_team_node_status requires an execution context.",
                reason="Run the tool from an active Team node.",
                permission=self.permission,
            )
        try:
            from app.agent_team.coordinator import get_agent_team_coordinator
            from app.core.runtime.conversation_run_cancellation_registry import (
                cancellation_registry,
            )

            result = get_agent_team_coordinator().submit_node_result(
                execution_context.run_id,
                status,
                output,
                runtime_loop=execution_context.runtime_dependencies.runtime_event_loop,
            )
            # 状态提交成功后，当前 ReAct workflow 不得再发起新的模型请求；下一节点由
            # coordinator 独立启动。工具执行层也会再次设置该信号，这里提前设置可覆盖
            # 单次工具批次内还有后续调用的情况。
            cancellation_registry.mark_cancelled(execution_context.run_id)
            return tool_success(
                tool_name=self.name,
                permission=self.permission,
                content=json.dumps(result, ensure_ascii=False),
            )
        except Exception as exc:
            return tool_error(
                self.name,
                "agent_team_node_status_rejected",
                reason=f"节点状态未被接受: {str(exc)[:500]}",
                permission=self.permission,
                retryable=False,
            )

    def to_definition(self) -> ToolDefinition:
        """构造节点专用状态工具定义。"""

        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="提交 Team 节点状态",
                icon="check",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )


def build_agent_team_node_status_definition() -> ToolDefinition:
    """构造节点状态工具定义。"""

    return AgentTeamNodeStatusTool().to_definition()
