"""Agent Team 执行工具（HITL ``execute``）：首次调用准备待确认方案并等待用户决定，按决定再次执行时
确认并阻塞运行 Team 直到落定，或落定驳回。"""

from __future__ import annotations

import asyncio
import json
from typing import cast

from app.config.logging.logger import log
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.tool.handler.agent_team.args.agent_team_args import AgentTeamArgs
from app.core.tool.handler.agent_team.agent_team_display import (
    build_agent_team_preview_display_data,
)
from app.core.tool.handler.agent_team.agent_team_result import team_result_payload
from app.core.tool.handler.agent_team.args.node_goal import AgentTeamNodeGoal
from app.core.tool.handler.agent_team.team_tool_error import TeamToolError
from app.core.tool.results.tool_awaiting_decision import tool_awaiting_decision
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.tool_names import TOOL_AGENT_TEAM
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_AGENT_TEAM
from app.core.workflow.inbox.task_message import DecisionBody
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.core.tool.handler.agent_team.args.agent_team_approve_input import AgentTeamApproveInput
from app.core.tool.handler.agent_team.agent_team_preparation_service import AgentTeamPreparationService
from app.core.tool.handler.agent_team.agent_team_run_approval import AgentTeamRunApproval
from app.core.tool.handler.agent_team.coordinator import get_agent_team_coordinator
from app.service.configuration.agent_team_configuration_registry import get_agent_team_registry
from app.service.depends import get_conversation_run_ledger


class AgentTeamRunTool(HandlerBase):
    """需要用户决定的 Agent Team 工具：一个入口按 ``execution_context.user_decision`` 分两段执行。

    - 首次调用（无决定）：准备方案、持久化为 pending TeamRun（记下本次 ``tool_call_id``），返回等待
      占位并挂起。
    - 按决定执行（``prepare_input`` 绕过模型重放原调用）：批准即确认（``pending → running``）并在本
      工具线程里阻塞运行 Team，落定后把结论作为工具结果返回；驳回即落定 ``user_rejected``。结果由
      通用 HITL 结算追加为 ``<tool_result>``，卡片随之收尾。

    Team 的生命周期因此包在这一次工具调用里：主 Agent 不需要 ``wait_messages``，也没有游离于 Run
    之外的后台执行。代价是 thread 工具执行中途不检查取消信号——主 Run 停止时由 ``TaskActor`` 在打
    信号的同时取消本 Run 的 TeamRun，Coordinator 检出后停下节点并返回，Run 才能走到检查点落定；
    用户在卡片上单独停止 Team 走 ``/agent-team/runs/{id}/cancel``，工具照常返回 cancelled 结论。

    审批门由后端强制：模型重复调用只会产生新的待确认方案，推进 TeamRun 只能经用户决定。
    """

    name = TOOL_AGENT_TEAM
    # 分工：描述只讲「这个工具做什么」与「它运行的 Team 如何执行」；字段级契约（要哪些 id、每个
    # id 给什么）写在 ``AgentTeamArgs`` 的 Field description 里，此处不复述——参数 schema 是模型
    # 填参时逐字段读到的文本，两处都写等于重复占 token。
    # 曾经的措辞「Prepare a configured Agent Team execution plan」与「配置 Team 草稿」语义重叠，
    # 实测让模型在用户说「帮我配置个 team」时反复选中本工具（它只运行已存在配置），因此显式声明
    # 它不能创建或修改配置。节点隔离语义必须留在描述里：模型据此才知道子目标要写成不依赖队友
    # 上下文的形式。节点的 JSON 交接协议是节点 Agent 自己的契约，主 Agent 控制不了，不写在这里。
    description = (
        "Run an existing Agent Team configuration for one goal. Its nodes run serially, each in "
        "its own task and context: they share only the workspace file system and the previous "
        "node's handoff output, never other nodes' conversations or earlier history, so each "
        "node's goal must be workable on its own. The ending node's output is the Team's final "
        "result. This tool submits a plan for the user to review and edit, then pauses you until "
        "the user decides; the decision arrives as a <user_decision> message. If approved, this "
        "call blocks until the whole Team finishes, which can take a long time; its <tool_result> "
        "reports the final status and the path of the saved run history, which you read in pages "
        "when you need the details. It cannot create or edit Team configurations; if the Team "
        "does not exist, ask the user to create it first."
    )
    args_model = AgentTeamArgs
    # thread 工具不强制超时（只是元数据）：批准后本调用阻塞到 Team 落定，耗时由 Team 的
    # max_runs 与节点自身预算约束，取消经 TeamRun 状态传入（见类说明）。
    group = TOOL_GROUP_AGENT_TEAM

    def __init__(self):
        self.agent_team_run_approval = AgentTeamRunApproval()
        self.agent_team_preparation_service = AgentTeamPreparationService()
        self.team_register = get_agent_team_registry()

    def execute(
            self,
            team_id: str,
            goal: str,
            node_goals: list[dict[str, str]],
            execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """无决定时准备待确认方案并等待；带着用户决定时按决定执行（见类说明）。

        消费运行时注入的父 Agent 运行时配置（本 Run 已物化模型设置的 per-run 副本），节点未自带
        模型连接配置时以它作为 fallback。**不得**改为从注册表重新解析：注册表里是共享单例，内置
        Agent 没有模型连接字段，会让 ``get_model_config_service().get_config(None)`` 抛
        ``KeyError``，使准备阶段整体失败。
        """

        if execution_context.user_decision is not None:
            return self._apply_decision(execution_context.user_decision, execution_context)
        # thread 工具的执行上下文恒带 Run 的 Agent 配置（只有 process 工具会剥离它）。
        parent_agent = cast(AgentRuntimeConfig, execution_context.agent)
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
        """准备并保存待确认 TeamRun（不启动执行），返回等待用户决定的占位观察。

        预览和运行时快照在同一次准备中生成，并在返回工具结果前写入主 SQLite；启动发生在用户
        批准之后（见 :meth:`_apply_decision`）。

        参数:
            parent_agent: 本次 Run 由运行时注入的父 Agent 运行时配置，供节点在缺少独立
                模型连接配置时回落（节点快照的模型设置与 ``model_config_id`` 都由它补全）。

        异常:
            无（失败路径归一化为 ``ToolObservation(status="error")``）。

        只把领域错误 ``TeamToolError`` 归一化为失败观察；其余异常是代码缺陷，交给工具执行层统一
        记录堆栈并转成失败观察，这里不再包一层。

        副作用:
            成功后向主 SQLite 写入一条 ``pending`` TeamRun；领域失败写 warning 日志。
        """

        try:
            configuration = self.team_register.resolve(
                str(execution_context.workspace_root), team_id
            )
            if configuration is None:
                return tool_error(
                    self.name,
                    f"The Team configuration does not exist: {team_id}; "
                    "please confirm whether this Team has been created",
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
            pending_run = self.agent_team_run_approval.create_pending_confirmation(
                configuration=configuration,
                preparation=preparation,
                workspace_id=execution_context.workspace_id,
                parent_task_id=execution_context.task_id,
                parent_run_id=execution_context.run_id,
                tool_call_id=execution_context.tool_call_id,
                goal=goal,
            )
            observation = tool_awaiting_decision(
                self.name,
                execution_context.tool_call_id,
                display_data=build_agent_team_preview_display_data(
                    preparation.preview_fields,
                    team_run_id=pending_run.id,
                ),
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
            # 可重试的是模型能自己改参数修好的错误（目标为空、节点目标与配置不符）；其余要用户
            # 修配置，提示模型别盲目重试。
            next_step = (
                "Fix the arguments and call again."
                if exc.retryable
                else "Ask the user to check the Team configuration; do not retry as is."
            )
            return tool_error(
                self.name,
                f"Failed to create the Team execution plan: {exc!s}. {next_step}",
            )

    def _apply_decision(
            self, decision: DecisionBody, execution_context: ToolExecutionContext
    ) -> ToolObservation:
        """按用户决定推进本次调用创建的 TeamRun；结果由通用 HITL 结算追加给模型。

        决定已在入队边界校验过，但从入队到这里之间 TeamRun 可能已被处理（主 Run 被取消时连同
        pending 方案一起取消、重复决定）：条件迁移落空时返回失败结果说明现状，不抛异常。
        节点回落的模型配置取自创建方案的主 Run 的持久化事实，而不是执行决定时的当前 Run——用户
        审批的是那次 Run 提出的方案。
        """
        from app.core.run_driver import resolve_agent_config

        row = self.agent_team_run_approval.get_for_tool_call(
            execution_context.task_id, execution_context.tool_call_id
        )
        if not decision.approved:
            current = self.agent_team_run_approval.reject_pending_confirmation(row.id)
            if current.end_reason != AgentTeamRunEndReason.USER_REJECTED.value:
                return self._already_resolved(row.id, current.status)
            observation = tool_success(
                self.name,
                json.dumps(
                    {
                        "status": "rejected",
                        "team_run_id": row.id,
                        "feedback": decision.feedback,
                        "next": (
                            "The Team will not run. If feedback is given, revise the plan "
                            "accordingly before submitting again; if not, ask the user what they "
                            "want instead of resubmitting the same plan."
                        ),
                    },
                    ensure_ascii=False,
                ),
            )
            observation.team_run_id = row.id
            return observation
        inputs = AgentTeamApproveInput.model_validate(decision.data)
        try:
            confirmed = self.agent_team_run_approval.confirm(
                row.id,
                inputs.configuration,
                goal=inputs.goal,
                node_goals=inputs.node_goals,
                parent_agent=resolve_agent_config(
                    get_conversation_run_ledger().get_run(row.parent_run_id)
                ),
            )
        except ValueError as exc:
            current = self.agent_team_run_approval.get(row.id)
            if current.status != AgentTeamRunStatus.PENDING.value:
                return self._already_resolved(row.id, current.status)
            log.warning(
                "agent_team_confirm_rejected",
                extra={
                    "msg": "用户批准的 Team 方案未通过确认校验",
                    "data": {"team_run_id": row.id, "reason": str(exc)},
                },
            )
            return tool_error(
                self.name,
                f"The approved plan could not be started: {exc}",
            )
        log.info(
            "agent_team_run_started",
            extra={
                "msg": "用户批准，Agent Team 开始运行",
                "data": {"team_run_id": row.id, "task_id": execution_context.task_id},
            },
        )
        final = get_agent_team_coordinator().start(
            confirmed, cast(asyncio.AbstractEventLoop, execution_context.event_loop)
        )
        observation = tool_success(
            self.name, json.dumps(team_result_payload(final), ensure_ascii=False)
        )
        observation.team_run_id = row.id
        return observation

    def _already_resolved(self, team_run_id: int, status: str) -> ToolObservation:
        return tool_error(
            self.name,
            f"This Team plan was already resolved (status: {status}) before the user's "
            "decision could be applied; nothing was started.",
        )

    def to_definition(self) -> ToolDefinition:
        """构造 Team 执行工具定义。"""

        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            execution_mode="thread",
            user_decision="execute",
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
