"""Agent Team 执行工具：准备方案并持久化为待确认 TeamRun，然后挂起等待用户决定。"""

from __future__ import annotations

import json
from typing import ClassVar

from app.core.tool.agent_team.node_goal import AgentTeamNodeGoal
from app.service.agent_team.registry import get_agent_team_registry
from app.core.tool.agent_team.team_tool_error import TeamToolError
from app.config.logging.logger import log
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.tool.agent_team.agent_team_display import build_agent_team_preview_display_data
from app.core.tool.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.tool_names import TOOL_AGENT_TEAM
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.tool_grouping import TOOL_GROUP_AGENT_TEAM
from app.core.tool.tool_base import HandlerBase
from app.core.tool.agent_team.agent_team_args import AgentTeamArgs
from app.service.agent_team.agent_team_preparation_service import AgentTeamPreparationService
from app.service.agent_team.agent_team_run_service import AgentTeamRunService


class AgentTeamRunTool(HandlerBase):
    """准备 Agent Team 执行方案，持久化为待确认 TeamRun 后挂起，由用户在卡片上决定。

    本工具只做准备：调用当步闭合并带 ``suspend``，Run 随即挂起。用户的批准 / 驳回由
    ``AgentTeamDecisionService`` 确定性执行（批准即后台启动 Team），再以消息唤醒 Agent；Team 结束后
    的结果另以后台结果消息送达。审批门因此由后端强制，模型无法自行把 TeamRun 推到运行。
    """

    name = TOOL_AGENT_TEAM
    # 分工：描述只讲「这个工具做什么」与「它运行的 Team 如何执行」；字段级契约（要哪些 id、每个
    # id 给什么）写在 ``AgentTeamArgs`` 的 Field description 里，此处不复述——参数 schema 是模型
    # 填参时逐字段读到的文本，两处都写等于重复占 token。
    # 曾经的措辞「Prepare a configured Agent Team execution plan」与「配置 Team 草稿」语义重叠，
    # 实测让模型在用户说「帮我配置个 team」时反复选中本工具（它只运行已存在配置），因此显式声明
    # 它不能创建或修改配置。节点执行语义必须留在描述里：模型据此才知道子目标要写成不依赖队友
    # 上下文的形式，并把下游所需信息显式写进输出。
    description = (
        "Run an existing Agent Team configuration for one goal. Its nodes run serially on one "
        "shared execution budget, each in its own task and context: they share only the workspace "
        "file system and the previous node's handoff output, never other nodes' conversations or "
        "earlier history. Every node must end by returning a JSON object with a status field (one "
        "of that node's allowed statuses) and an output field: the status alone selects the next "
        "node or ends the Team, any other final output fails the Team, and the ending node's "
        "output is the Team's final result. This tool submits a plan for the user to review and "
        "edit, then pauses you: the user's decision arrives as a <user_decision> message, and if "
        "approved the Team runs in the background and its final status arrives as a "
        "<background_result> message. It cannot create or edit Team configurations; if the Team "
        "does not exist, ask the user to create it first."
    )
    args_model = AgentTeamArgs
    timeout_seconds: ClassVar[float] = 30.0
    group = TOOL_GROUP_AGENT_TEAM

    def __init__(self):
        self.agent_team_run_service = AgentTeamRunService()
        self.agent_team_preparation_service = AgentTeamPreparationService()
        self.team_register = get_agent_team_registry()

    def execute(
            self,
            team_id: str,
            goal: str,
            node_goals: list[dict[str, str]],
            execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """准备待确认方案并挂起。

        消费运行时注入的父 Agent 运行时配置（本 Run 已物化模型设置的 per-run 副本），节点未自带
        模型连接配置时以它作为 fallback。**不得**改为从注册表重新解析：注册表里是共享单例，内置
        Agent 没有模型连接字段，会让 ``get_model_config_service().get_config(None)`` 抛
        ``KeyError``，使准备阶段整体失败。
        """

        parent_agent = execution_context.agent
        if parent_agent is None:
            log.warning(
                "agent_team_parent_profile_unavailable",
                extra={
                    "msg": "本次 Run 未注入父 Agent 运行时配置，无法准备 Team 运行",
                    "data": {
                        "team_id": team_id,
                        "run_id": execution_context.run_id,
                        "task_id": execution_context.task_id,
                    },
                },
            )
            return tool_error(
                self.name,
                error="agent_team_runtime_unavailable",
                reason=(
                    "The parent Agent profile was not injected into this run, so the Team "
                    "execution plan cannot be prepared."
                ),
                retryable=False,
            )
        # 参数入口已检查 node_id 唯一性。
        goals_by_node = {
            item["node_id"]: AgentTeamNodeGoal(goal=item["goal"], summary=item["summary"])
            for item in node_goals
        }
        return self._prepare_pending_run(
            team_id, goal, goals_by_node, execution_context, parent_agent
        )

    def _prepare_pending_run(
            self,
            team_id: str,
            goal: str,
            node_goals: dict[str, AgentTeamNodeGoal],
            execution_context: ToolExecutionContext,
            parent_agent: AgentRuntimeConfig,
    ) -> ToolObservation:
        """准备并保存待确认 TeamRun（不启动执行），返回带 ``suspend`` 的成功观察。

        预览和运行时快照在同一次准备中生成，并在返回工具结果前写入主 SQLite；启动发生在用户
        批准之后（见 ``AgentTeamDecisionService``）。

        参数:
            parent_agent: 本次 Run 由运行时注入的父 Agent 运行时配置，供节点在缺少独立
                模型连接配置时回落（节点快照的模型设置与 ``model_config_id`` 都由它补全）。

        异常:
            无（失败路径归一化为 ``ToolObservation(status="error")``）。

        副作用:
            成功后向主 SQLite 写入一条 ``pending`` TeamRun；失败路径写 warning / error 日志
            （未知异常带堆栈，避免只留一个 ``error:None``）。
        """

        try:

            configuration = self.team_register.resolve(
                str(execution_context.workspace_root), team_id.strip()
            )
            if configuration is None:
                return tool_error(
                    self.name,
                    error="agent_team_run_creation_invalid",
                    reason=(
                        f"The Team configuration does not exist: {team_id}; "
                        "please confirm whether this Team has been created"
                    ),
                    retryable=False,
                )
            preparation = self.agent_team_preparation_service.prepare(
                configuration,
                goal=goal,
                node_goals=node_goals,
                workspace_root=str(execution_context.workspace_root),
                parent_task_id=execution_context.task_id,
                parent_run_id=execution_context.run_id,
                workspace_id=execution_context.workspace_id,
                parent_agent=parent_agent,
            )
            pending_run = self.agent_team_run_service.create_pending_confirmation(
                configuration=configuration,
                preparation=preparation,
                workspace_id=execution_context.workspace_id,
                parent_task_id=execution_context.task_id,
                parent_run_id=execution_context.run_id,
                goal=goal,
            )
            observation = tool_success(
                tool_name=self.name,
                content=json.dumps(
                    {
                        "status": "awaiting_user_review",
                        "team_id": configuration.team_id,
                        "team_run_id": pending_run.id,
                        "next": (
                            "You are paused until the user decides; do not call this tool "
                            "again for the same plan."
                        ),
                    },
                    ensure_ascii=False,
                ),
                display_data=build_agent_team_preview_display_data(
                    preparation.preview_fields,
                    team_run_id=pending_run.id,
                ),
                suspend=True,
            )
            observation.team_run_id = pending_run.id
            return observation
        except TeamToolError as exc:
            log.warning(
                "agent_team_run_creation_failed",
                extra={
                    "msg": "Agent Team 待确认运行创建失败",
                    "data": {
                        "team_id": team_id,
                        "run_id": execution_context.run_id,
                        "error_type": type(exc).__name__,
                        "retryable": exc.retryable,
                        "reason": str(exc),
                    },
                },
            )
            return tool_error(
                self.name,
                "agent_team_run_creation_invalid",
                reason=(
                    "Failed to create the Team execution plan. Please check the Team "
                    f"configuration or retry later. error:{exc!s}"
                ),
                retryable=exc.retryable,
            )
        except Exception as exc:
            # 兜底分支只应捕获代码缺陷：堆栈是唯一能定位它的证据，必须带 exc_info 落盘。
            log.error(
                "agent_team_run_creation_failed",
                extra={
                    "msg": f"Agent Team 待确认运行创建失败（未预期异常 {type(exc).__name__}）",
                    "data": {
                        "team_id": team_id,
                        "run_id": execution_context.run_id,
                        "task_id": execution_context.task_id,
                    },
                },
                exc_info=True,
            )
            return tool_error(
                self.name,
                "agent_team_run_creation_invalid",
                reason=(
                    "Preparing the Team execution plan failed with an internal error "
                    f"({type(exc).__name__}: {exc}); the Team configuration itself may be valid, "
                    "so do not retry this call."
                ),
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
                # verb 与同组 propose 工具的中文口径一致；原英文 "Prepare Agent Team
                # execution plan" 会被读成「配置 Team」，与工具实际职责不符。
                verb="运行已有 Agent Team",
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
