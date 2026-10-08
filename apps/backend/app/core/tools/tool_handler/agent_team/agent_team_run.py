"""Agent Team 执行工具：准备方案并持久化为待确认 TeamRun。"""

from __future__ import annotations

import json
from typing import ClassVar

from app.agent_team.registry import get_agent_team_registry
from app.agent_team.team_tool_error import TeamToolError
from app.config.configuration import get_agent_registry
from app.config.logging.logger import log
from app.core.tools.display.agent_team_display import build_agent_team_preview_display_data
from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.schemas.tool_names import TOOL_AGENT_TEAM, TOOL_PROPOSE_AGENT_TEAM_CONFIGURATION
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_grouping import TOOL_GROUP_AGENT_TEAM
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import AgentTeamArgs
from app.service.agent_team.agent_team_preparation_service import AgentTeamPreparationService
from app.service.agent_team.agent_team_run_service import AgentTeamRunService
from app.service.depends import get_conversation_run_service


class AgentTeamRunTool(HandlerBase):
    """准备 Agent Team 执行方案，并创建等待用户确认的持久化 TeamRun。"""

    name = TOOL_AGENT_TEAM
    description = (
        "Prepare a configured Agent Team execution plan for the user to review. "
        "The referenced team must already exist; if it does not, ask the user to create it first. "
        "The plan requires explicit user confirmation before execution; once the user confirms, "
        "the Agent Team starts the TeamRun."
    )
    args_model = AgentTeamArgs
    timeout_seconds: ClassVar[float] = 30.0
    group = TOOL_GROUP_AGENT_TEAM

    def __init__(self):
        self.agent_team_run_service = AgentTeamRunService()
        self.agent_team_preparation_service = AgentTeamPreparationService()
        self.run_service = get_conversation_run_service()
        self.team_register = get_agent_team_registry()

    def execute(
        self,
        team_id: str,
        goal: str,
        node_goals: dict[str, str],
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """准备并保存当前主 Agent Run 的待确认 TeamRun。

        预览和运行时快照在同一次准备中生成，并在返回工具结果前写入主 SQLite。此方法不
        启动 TeamRun；启动发生在用户确认执行方案之后。准备或持久化失败会抛出
        ``TeamToolError``，由下方 ``except`` 统一映射为不可重试的失败观察。
        """

        if execution_context is None:
            return tool_error(
                self.name,
                "agent_team requires an execution context.",
                reason="Run the tool from an active Agent context.",
            )
        try:

            configuration = self.team_register.resolve(
                str(execution_context.workspace_root), team_id.strip()
            )
            if configuration is None:
                return tool_error(
                    self.name,
                    error="agent_team_run_creation_invalid",
                    reason=f"The Team configuration does not exist: {team_id}; please confirm whether this Team has been created",
                    retryable=False
                )
            parent_run = self.run_service.get_run(execution_context.run_id)
            # Runner 为主 Run 派生的 profile 已经物化了模型连接配置。节点缺少独立配置时，
            # 只能沿用这份本次 Run 快照，不能在确认时重新读取可变配置。
            parent_model_settings = (
                execution_context.runtime_dependencies.parent_agent_profile.model_settings
                if execution_context.runtime_dependencies.parent_agent_profile is not None
                else None
            )
            preparation = self.agent_team_preparation_service.prepare(
                configuration,
                goal=goal,
                node_goals=node_goals,
                workspace_root=str(execution_context.workspace_root),
                parent_task_id=execution_context.task_id,
                parent_run_id=execution_context.run_id,
                workspace_id=execution_context.workspace_id,
                parent_agent_profile=get_agent_registry().resolve(execution_context.workspace_root,parent_run.agent_id),
            )
            pending_run = self.agent_team_run_service.create_pending_confirmation(
                configuration=configuration,
                preparation=preparation,
                workspace_id=execution_context.workspace_id,
                parent_task_id=execution_context.task_id,
                parent_run_id=execution_context.run_id,
                goal=goal,
            )
            return tool_success(
                tool_name=self.name,
                content=json.dumps(
                    {
                        "status": "pending",
                        "team_id": configuration.team_id,
                    },
                    ensure_ascii=False,
                ),
                display_data=build_agent_team_preview_display_data(
                    preparation.preview_fields,
                    team_run_id=pending_run.id,
                ),
            )
        except TeamToolError as exc:
            log.warning(
                "agent_team_run_creation_failed",
                extra={
                    "msg": "Agent Team 待确认运行创建失败",
                    "data": {
                        "team_id": team_id,
                        "error_type": type(exc).__name__,
                        "retryable": exc.retryable,
                    },
                },
            )
            return tool_error(
                self.name,
                "agent_team_run_creation_invalid",
                reason=f"无法创建 Team 执行方案，请检查 Team 配置或稍后重试,error:{str(exc)}",
                retryable=exc.retryable,
            )
        except Exception as exc:
            log.warning(
                "agent_team_run_creation_failed",
                extra={
                    "msg": "Agent Team 待确认运行创建失败（未知异常）",
                    "data": {"team_id": team_id, "error_type": type(exc).__name__},
                },
            )
            return tool_error(
                self.name,
                "agent_team_run_creation_invalid",
                reason=f"无法创建 Team 执行方案，请检查 Team 配置或稍后重试,error:{str(exc)}",
                retryable=False,
            )

    def to_definition(self) -> ToolDefinition:
        """构造 Team 执行工具定义。"""

        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
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
