"""Agent Team 执行工具：准备方案并持久化为待确认 TeamRun。"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import ValidationError

from app.agent_team.registry import get_agent_team_registry
from app.agent_team.team_tool_error import TeamToolError
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.tools.display.agent_team_display import (
    build_agent_team_preview_display_data,
    build_agent_team_run_display_data,
)
from app.core.tools.schemas import (
    EXECUTING_DECISION_KINDS,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
    UserDecision,
)
from app.core.tools.schemas.tool_names import TOOL_AGENT_TEAM
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_grouping import TOOL_GROUP_AGENT_TEAM
from app.core.tools.tool_handler.agent_team.review_request import build_team_review_request
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import AgentTeamApproveInput, AgentTeamArgs
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.service.agent_team.agent_team_preparation_service import AgentTeamPreparationService
from app.service.agent_team.agent_team_run_service import AgentTeamRunService


class AgentTeamRunTool(HandlerBase):
    """准备 Agent Team 执行方案，并在用户批准后启动该 TeamRun。

    两阶段的执行语义见 :meth:`execute`：第一遍落待确认的持久化 TeamRun 并把决定权交给用户，
    第二遍（用户批准后由 ``tools`` 节点重执行）才真正启动执行。
    """

    name = TOOL_AGENT_TEAM
    # 描述刻意只讲「运行一个已存在的 Team」：曾经的措辞「Prepare a configured Agent Team
    # execution plan」与「配置 Team 草稿」语义重叠，实测让模型在用户说「帮我配置个 team」时
    # 反复选中本工具（而它只运行已存在配置），因此显式声明它不能创建或修改 Team 配置。
    description = (
        "Run an existing Agent Team: prepare its execution plan for the user to review, then "
        "start the TeamRun once the user confirms. "
        "The referenced team must already exist; this tool cannot create or modify Team "
        "configurations, so if the team does not exist, stop and tell the user to create it first. "
        "This tool must be invoked serially and cannot run in parallel with other tools."
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
        node_goals: dict[str, str],
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """准备待确认方案，或在用户批准后真正启动该 TeamRun。

        本工具是两阶段的 human-in-the-loop 工具：第一遍（``execution_context.user_decision``
        为 ``None``）只做准备并把决定权交给用户；用户批准后 ``tools`` 节点以同一调用重新执行，
        此时决定非空，进入真正的启动分支。分支依据由框架注入（模型不可写），因此不能用参数
        伪造「已获批准」；驳回与放弃不会重执行本调用（它们在 ``wait_user`` 节点即被翻译成观察）。

        两遍都消费运行时注入的父 Agent profile（本 Run 已物化模型设置的 per-run 副本），节点
        未自带模型连接配置时以它作为 fallback。**不得**改为从注册表重新解析：注册表里是共享
        单例，内置 Agent 没有模型连接字段，会让 ``get_model_config_service().get_config(None)``
        抛 ``KeyError``，使准备阶段整体失败。
        """

        if execution_context is None:
            return tool_error(
                self.name,
                "agent_team requires an execution context.",
                reason="Run the tool from an active Agent context.",
            )
        parent_agent_profile = execution_context.runtime_dependencies.parent_agent_profile
        if parent_agent_profile is None:
            log.warning(
                "agent_team_parent_profile_unavailable",
                extra={
                    "msg": "本次 Run 未注入父 Agent profile，无法准备 Team 运行",
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
        decision = execution_context.user_decision
        if decision is not None and decision.kind in EXECUTING_DECISION_KINDS:
            return self._start_confirmed_run(decision, parent_agent_profile)
        return self._prepare_pending_run(
            team_id, goal, node_goals, execution_context, parent_agent_profile
        )

    def _prepare_pending_run(
        self,
        team_id: str,
        goal: str,
        node_goals: dict[str, str],
        execution_context: ToolExecutionContext,
        parent_agent_profile: AgentProfile,
    ) -> ToolObservation:
        """第一遍：准备并保存待确认 TeamRun（不启动执行）。

        预览和运行时快照在同一次准备中生成，并在返回工具结果前写入主 SQLite；启动发生在用户
        批准之后（见 :meth:`_start_confirmed_run`）。

        参数:
            parent_agent_profile: 本次 Run 由运行时注入的父 Agent profile，供节点在缺少独立
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
                    reason=f"The Team configuration does not exist: {team_id}; please confirm whether this Team has been created",
                    retryable=False
                )
            preparation = self.agent_team_preparation_service.prepare(
                configuration,
                goal=goal,
                node_goals=node_goals,
                workspace_root=str(execution_context.workspace_root),
                parent_task_id=execution_context.task_id,
                parent_run_id=execution_context.run_id,
                workspace_id=execution_context.workspace_id,
                parent_agent_profile=parent_agent_profile,
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
                # 待用户确认的事实经观察的专用字段下发（不是展示数据）：工作流据此挂起图，
                # 展示模块只负责卡片本身。
                user_input_request=build_team_review_request(
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
                reason=f"Failed to create the Team execution plan. Please check the Team configuration or retry later. error:{str(exc)}",
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

    def _start_confirmed_run(
        self,
        decision: UserDecision,
        parent_agent_profile: AgentProfile,
    ) -> ToolObservation:
        """第二遍：用户批准后启动 TeamRun 并返回启动结果。

        ``decision.request_id`` 是待确认 TeamRun 的主键（第一遍由本工具写入
        ``user_input_request.request_id``），``decision.data`` 是用户在卡片上编辑后的最终运行输入。
        配置的领域校验在 ``confirm_and_start`` 边界完成，本方法只做形状校验与失败归一化。

        幂等：重复执行（图重放、用户重试）由 ``confirm_and_start`` 的条件状态迁移兜住，已进入
        运行 / 终态的 TeamRun 会原样返回当前事实，不重复启动 Coordinator。

        参数:
            decision: 框架注入的用户决定（批准）。
            parent_agent_profile: 本次 Run 由运行时注入的父 Agent profile；确认边界会用它重新
                生成节点运行快照（见 :meth:`_prepare_pending_run` 的同名说明）。

        返回:
            启动成功或「已启动」时为 ``success`` 观察；TeamRun 不存在、已被取消 / 失败，或输入
            形状非法时为 ``error`` 观察（不抛出）。

        异常:
            无（失败路径归一化为 ``ToolObservation(status="error")``）。

        副作用:
            经 ``AgentTeamRunService.confirm_and_start`` 把 TeamRun 从 ``pending`` 原子迁移为
            ``running`` 并启动 Coordinator；写 ``agent_team_confirmation_failed`` 日志。
        """

        try:
            team_run_id = int(decision.request_id)
        except (TypeError, ValueError):
            return self._confirmation_error(f"Invalid TeamRun identifier: {decision.request_id}")
        try:
            inputs = AgentTeamApproveInput.model_validate(decision.data)
        except ValidationError as exc:
            return self._confirmation_error(f"Invalid confirmation input: {exc.error_count()} field errors")
        try:
            row = self.agent_team_run_service.confirm_and_start(
                team_run_id,
                inputs.configuration,
                goal=inputs.goal,
                node_goals=inputs.node_goals,
                parent_agent_profile=parent_agent_profile,
            )
        except KeyError:
            return self._confirmation_error(f"The pending TeamRun does not exist: {team_run_id}")
        except ValueError as exc:
            return self._confirmation_error(str(exc))
        if row.status in {
            AgentTeamRunStatus.CANCELLED.value,
            AgentTeamRunStatus.FAILED.value,
        }:
            # 条件迁移未生效且行已终态：多为后端重启把 pending 收敛为 cancelled。此时不能报告
            # 成功，否则模型会继续等一个永远不会运行的 Team。
            return self._confirmation_error(f"This Team run has already ended ({row.status})")
        return tool_success(
            tool_name=self.name,
            content=json.dumps(
                {
                    "status": row.status,
                    "team_id": row.team_id,
                },
                ensure_ascii=False,
            ),
            display_data=build_agent_team_run_display_data(
                team_run_id=row.id,
                team_id=row.team_id,
                goal=inputs.goal,
                node_goals=inputs.node_goals,
            ),
        )

    def _confirmation_error(self, reason: str) -> ToolObservation:
        """把确认阶段的失败归一化为不重试的错误观察，并留下可排查日志。"""

        log.warning(
            "agent_team_confirmation_failed",
            extra={
                "msg": "Agent Team 确认执行失败",
                "data": {"tool": self.name, "reason": reason},
            },
        )
        return tool_error(
            self.name,
            "agent_team_confirmation_invalid",
            reason=f"Failed to start the Team execution plan: {reason}",
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
            need_HIL=True,
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
