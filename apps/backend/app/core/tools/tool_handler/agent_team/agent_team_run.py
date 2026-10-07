"""Agent Team 执行工具：准备方案并持久化为待确认 TeamRun。"""

from __future__ import annotations

import json
from typing import ClassVar

from app.agent_team.registry import get_agent_team_registry
from app.config.logging.logger import log
from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.schemas.tool_names import TOOL_AGENT_TEAM
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_grouping import TOOL_GROUP_AGENT_TEAM
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import AgentTeamArgs
from app.service.depends import get_conversation_run_state_service


class AgentTeamRunTool(HandlerBase):
    """准备 Agent Team 执行方案，并创建等待用户确认的持久化 TeamRun。"""

    name = TOOL_AGENT_TEAM
    description = (
        "Prepare a configured Agent Team execution plan. "
        "It requires user confirmation before execution."
    )
    permission: ClassVar[str] = "agent_team"
    args_model = AgentTeamArgs
    timeout_seconds: ClassVar[float] = 30.0
    risk_level: ClassVar[str] = "medium"
    group = TOOL_GROUP_AGENT_TEAM

    def execute(
        self,
        team_id: str,
        goal: str,
        instructions: dict[str, str] | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """准备并保存当前主 Agent Run 的待确认 TeamRun。

        预览和运行时快照在同一次准备中生成，并在返回工具结果前写入主 SQLite。此方法不
        启动 TeamRun；启动发生在用户确认执行方案之后。
        """

        if execution_context is None:
            return tool_error(
                self.name,
                "agent_team requires an execution context.",
                reason="Run the tool from an active Agent context.",
                permission=self.permission,
            )
        try:
            # 准备服务会读取 ToolSystem；延迟导入以避免 ToolSystem 装配 Team 工具时形成循环导入。
            from app.service.agent_team.agent_team_preparation_service import (
                AgentTeamPreparationService,
            )
            from app.service.agent_team.agent_team_run_service import (
                AgentTeamRunService,
            )

            configuration = get_agent_team_registry().resolve(
                str(execution_context.workspace_root), team_id.strip()
            )
            if configuration is None:
                raise ValueError(f"Team 配置不存在: {team_id}")
            parent_run = get_conversation_run_state_service().get_run(execution_context.run_id)
            # Runner 为主 Run 派生的 profile 已经物化了模型连接配置。节点缺少独立配置时，
            # 只能沿用这份本次 Run 快照，不能在确认时重新读取可变配置。
            fallback_model_settings = (
                execution_context.runtime_dependencies.parent_agent_profile.model_settings
                if execution_context.runtime_dependencies.parent_agent_profile is not None
                else None
            )
            preparation = AgentTeamPreparationService().prepare(
                configuration,
                goal=goal,
                instructions=instructions or {},
                workspace_root=str(execution_context.workspace_root),
                parent_task_id=execution_context.task_id,
                parent_run_id=execution_context.run_id,
                workspace_id=execution_context.workspace_id,
                fallback_model_settings=fallback_model_settings,
                fallback_model_config_id=parent_run.model_config_id,
            )
            pending_run = AgentTeamRunService().create_pending_confirmation(
                configuration=configuration,
                preparation=preparation,
                workspace_id=execution_context.workspace_id,
                parent_task_id=execution_context.task_id,
                parent_run_id=execution_context.run_id,
                goal=goal,
                instructions=instructions or {},
            )
            return tool_success(
                tool_name=self.name,
                permission=self.permission,
                content=json.dumps(
                    {
                        "status": "pending",
                        "team_id": configuration.team_id,
                    },
                    ensure_ascii=False,
                ),
                display_data={
                    **preparation.preview_document,
                    "team_run_id": pending_run.id,
                },
            )
        except Exception as exc:
            log.warning(
                "agent_team_run_creation_failed",
                extra={
                    "msg": "Agent Team 待确认运行创建失败",
                    "data": {"team_id": team_id, "error_type": type(exc).__name__},
                },
            )
            return tool_error(
                self.name,
                "agent_team_run_creation_invalid",
                reason=f"无法创建 Team 执行方案: {str(exc)[:500]}",
                permission=self.permission,
                retryable=True,
            )

    def to_definition(self) -> ToolDefinition:
        """构造 Team 执行工具定义。"""

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
                verb="准备 Agent Team 执行方案",
                icon="workflow",
                surface="standalone",
                expandable=True,
                expand_layout="details",
                show_result=False,
            ),
        )


def build_agent_team_run_definition() -> ToolDefinition:
    """构造 Team 执行工具定义。"""

    return AgentTeamRunTool().to_definition()
