"""ProposeAgentTeamConfigurationTool：生成不落盘的 Team 配置候选。"""

from __future__ import annotations

from typing import ClassVar

from app.config.logging.logger import log

from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.schemas.tool_names import TOOL_PROPOSE_AGENT_TEAM_CONFIGURATION
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_grouping import TOOL_GROUP_AGENT_TEAM
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import ProposeAgentTeamConfigurationArgs


class ProposeAgentTeamConfigurationTool(HandlerBase):
    """生成不落盘的 Team 配置候选。"""

    name = TOOL_PROPOSE_AGENT_TEAM_CONFIGURATION
    description = (
        "Create an Agent Team configuration draft for user review. Do not save or execute it."
    )
    permission: ClassVar[str] = "agent_team_configuration_proposal"
    args_model = ProposeAgentTeamConfigurationArgs
    timeout_seconds: ClassVar[float] = 20.0
    group = TOOL_GROUP_AGENT_TEAM

    def execute(
        self,
        execution_context: ToolExecutionContext | None = None,
        **kwargs: object,
    ) -> ToolObservation:
        """校验配置候选并返回前端可审阅的展示数据。"""

        try:
            from app.core.tools.display.agent_team_display import (
                build_agent_team_configuration_display_data,
            )
            from app.service.agent_team.agent_team_preparation_service import (
                resolve_node_profile,
            )

            # 提案只生成待审阅的工具输入预览，不提前构造持久化配置对象。用户确认
            # 保存时，保存 API 会用用户实际选择的 scope 重新校验领域配置。
            proposal: ProposeAgentTeamConfigurationArgs = self.args_model.model_validate(kwargs)
            if execution_context is not None:
                for node in proposal.nodes:
                    resolve_node_profile(
                        proposal,
                        str(execution_context.workspace_root),
                        node.node_id,
                    )
            return tool_success(
                tool_name=self.name,
                permission=self.permission,
                content="Agent Team configuration draft is ready for user review.",
                display_data=build_agent_team_configuration_display_data(
                    proposal,
                    scope="workspace",
                ),
            )
        except Exception as exc:
            log.warning(
                "agent_team_configuration_proposal_failed",
                extra={"msg": "Agent Team 配置提案校验失败", "error_type": type(exc).__name__},
            )
            return tool_error(
                tool_name=self.name,
                error="agent_team_configuration_invalid",
                reason="The Team configuration is invalid. Please review and try again.",
                permission=self.permission,
                retryable=True,
            )

    def to_definition(self) -> ToolDefinition:
        """构造配置候选工具定义。"""

        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="生成 Agent Team 配置草稿",
                icon="users",
                surface="standalone",
                expandable=True,
                expand_layout="details",
                show_result=False,
            ),
        )


def build_propose_agent_team_configuration_definition() -> ToolDefinition:
    """构造 Team 配置候选工具定义。"""

    return ProposeAgentTeamConfigurationTool().to_definition()
