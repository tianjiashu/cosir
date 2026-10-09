"""Agent Team 执行工具：准备方案并持久化为待确认 TeamRun。"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import ValidationError

from app.agent_team.registry import get_agent_team_registry
from app.agent_team.team_tool_error import TeamToolError
from app.config.configuration import get_agent_registry
from app.config.logging.logger import log
from app.core.tools.display.agent_team_display import (
    build_agent_team_preview_display_data,
    build_agent_team_run_display_data,
)
from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
    UserDecision,
)
from app.core.tools.schemas.tool_names import TOOL_AGENT_TEAM, TOOL_PROPOSE_AGENT_TEAM_CONFIGURATION
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_grouping import TOOL_GROUP_AGENT_TEAM
from app.core.tools.tool_handler.agent_team.review_request import build_team_review_request
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import AgentTeamApproveInput, AgentTeamArgs
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.service.agent_team.agent_team_preparation_service import AgentTeamPreparationService
from app.service.agent_team.agent_team_run_service import AgentTeamRunService
from app.service.depends import get_conversation_run_service


class AgentTeamRunTool(HandlerBase):
    """准备 Agent Team 执行方案，并在用户批准后启动该 TeamRun。

    两阶段的执行语义见 :meth:`execute`：第一遍落待确认的持久化 TeamRun 并把决定权交给用户，
    第二遍（用户批准后由 ``tools`` 节点重执行）才真正启动执行。
    """

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
        """准备待确认方案，或在用户批准后真正启动该 TeamRun。

        本工具是两阶段的 human-in-the-loop 工具：第一遍（``execution_context.user_decision``
        为 ``None``）只做准备并把决定权交给用户；用户批准后 ``tools`` 节点以同一调用重新执行，
        此时决定非空，进入真正的启动分支。分支依据由框架注入（模型不可写），因此不能用参数
        伪造「已获批准」；驳回与放弃不会重执行本调用（它们在 ``wait_user`` 节点即被翻译成观察）。
        """

        if execution_context is None:
            return tool_error(
                self.name,
                "agent_team requires an execution context.",
                reason="Run the tool from an active Agent context.",
            )
        if execution_context.user_decision is not None and execution_context.user_decision.kind == UserDecision.Kind.APPROVE:
            return self._start_confirmed_run(execution_context.user_decision)
        return self._prepare_pending_run(team_id, goal, node_goals, execution_context)

    def _prepare_pending_run(
        self,
        team_id: str,
        goal: str,
        node_goals: dict[str, str],
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """第一遍：准备并保存待确认 TeamRun（不启动执行）。

        预览和运行时快照在同一次准备中生成，并在返回工具结果前写入主 SQLite；启动发生在用户
        批准之后（见 :meth:`_start_confirmed_run`）。准备或持久化失败会抛出 ``TeamToolError``，
        由下方 ``except`` 统一映射为不可重试的失败观察。

        异常:
            无（失败路径归一化为 ``ToolObservation(status="error")``）。
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

    def _start_confirmed_run(self, decision: UserDecision) -> ToolObservation:
        """第二遍：用户批准后启动 TeamRun 并返回启动结果。

        ``decision.request_id`` 是待确认 TeamRun 的主键（第一遍由本工具写入
        ``user_input_request.request_id``），``decision.data`` 是用户在卡片上编辑后的最终运行输入。
        配置的领域校验在 ``confirm_and_start`` 边界完成，本方法只做形状校验与失败归一化。

        幂等：重复执行（图重放、用户重试）由 ``confirm_and_start`` 的条件状态迁移兜住，已进入
        运行 / 终态的 TeamRun 会原样返回当前事实，不重复启动 Coordinator。

        参数:
            decision: 框架注入的用户决定（批准）。

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
            return self._confirmation_error(f"无效的 TeamRun 标识：{decision.request_id}")
        try:
            inputs = AgentTeamApproveInput.model_validate(decision.data)
        except ValidationError as exc:
            return self._confirmation_error(f"确认输入不合法：{exc.error_count()} 处字段错误")
        try:
            row = self.agent_team_run_service.confirm_and_start(
                team_run_id,
                inputs.configuration,
                goal=inputs.goal,
                node_goals=inputs.node_goals,
            )
        except KeyError:
            return self._confirmation_error(f"待确认的 TeamRun 不存在：{team_run_id}")
        except ValueError as exc:
            return self._confirmation_error(str(exc))
        if row.status in {
            AgentTeamRunStatus.CANCELLED.value,
            AgentTeamRunStatus.FAILED.value,
        }:
            # 条件迁移未生效且行已终态：多为后端重启把 pending 收敛为 cancelled。此时不能报告
            # 成功，否则模型会继续等一个永远不会运行的 Team。
            return self._confirmation_error(f"该 Team 运行已结束（{row.status}），请重新提案")
        return tool_success(
            tool_name=self.name,
            content=json.dumps(
                {
                    "status": row.status,
                    "team_id": row.team_id,
                    "team_run_id": row.id,
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
            reason=f"无法启动 Team 执行方案：{reason}",
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
