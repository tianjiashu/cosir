"""Agent Team 节点调度、结果提交和终态收敛。"""

from __future__ import annotations

import asyncio
import copy
import json
from collections import defaultdict
from threading import RLock

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.configuration.team_node_definition import TeamNodeDefinition
from app.agent_team.node_profile import NODE_PROFILE_SNAPSHOT_KEY, build_node_profile
from app.agent_team.state.agent_team_run_state import (
    AgentTeamNodeExecution,
    AgentTeamRunRuntime,
    AgentTeamRunState,
)
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.models import ConversationRunRecord
from app.models.conversation_run_command import ConversationRunCommand
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service.agent_team.agent_team_run_archive_service import AgentTeamRunArchiveService
from app.service.depends import (
    get_conversation_run_service,
    get_task_service,
    get_workflow_session,
    get_workspace_service,
)
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.storage.store_engines import main_session_factory

# 等待节点释放期间检查父 Task 取消信号的间隔；与工具执行层的取消轮询同量级。
_NODE_CANCEL_POLL_SECONDS = 0.2


class AgentTeamCoordinator:
    """拥有 TeamRun 的节点调度状态机，节点 Run 经 ``WorkflowSession.submit`` 启动。

    每条 Team 由一个异步驱动任务串行执行节点：Coordinator 等待节点 Run 结束、读取其
    持久化 ``final_output``、结算 transition 并创建下一节点。Executor 只执行单个 Run；主
    ConversationRun 的等待与恢复交给 ``AgentTeamParentRunService``。
    """

    def __init__(self) -> None:

        from app.config.configuration import get_agent_registry, get_tool_system

        self._team_run_crud = AgentTeamRunCrud()
        self._task_service = get_task_service()
        self._tool_system = get_tool_system()
        self._run_service = get_conversation_run_service()
        self._session_factory = main_session_factory()
        self._locks_guard = RLock()
        self._locks: dict[int, RLock] = defaultdict(RLock)
        self._workspace_service = get_workspace_service()
        self._agent_registry = get_agent_registry()
        self._run_archive_service = AgentTeamRunArchiveService()

    def start(
            self,
            team_run: AgentTeamRunModel,
            event_loop: asyncio.AbstractEventLoop,
    ) -> AgentTeamRunModel:
        """在调用方线程内串行驱动整条已确认的 TeamRun，直到 Team 落定终态。

        本方法只接受已经持久化且完成确认的 ``running`` TeamRun，不读取待确认请求、预览
        文档或用户确认参数。TeamRun 的创建、确认和 ``pending`` 到 ``running`` 的原子迁移
        由执行请求 service 完成。调用方是 ``AgentTeamDecisionService`` 的后台任务（工作线程）；
        节点 Run 经 ``WorkflowSession.submit`` 在主事件循环 ``event_loop`` 上启动，本线程只等待其
        释放。

        取消：以 TeamRun 自身状态为准，不读父 Task 的取消信号——Team 在后台执行时父 Run 可能处于
        挂起，没有执行者也就没有信号。父 Agent 取消、用户在卡片上取消都经 :meth:`cancel` 把
        TeamRun 落定 cancelled；本循环每轮开始前与节点结束后检查，等待节点期间检出则停止节点。

        返回:
            落定终态后的 TeamRun 快照。

        异常:
            KeyError: TeamRun 不存在。
            ValueError: Team 已处于非运行态或已有活动节点（重复启动），或已冻结的节点配置或
                运行快照无效。
        """
        state: AgentTeamRunState | None = None
        try:
            if team_run.status != AgentTeamRunStatus.RUNNING.value:
                raise ValueError(f"Team 已进入非运行态({team_run.status})，无法启动")
            state = AgentTeamRunState.model_validate(team_run.state_json)
            configuration = AgentTeamConfiguration.model_validate(
                team_run.configuration_snapshot_json
            )

            state.current_node_id = configuration.start_node_id
            next_node_input = None
            rounds = 0

            while True:
                if self._stopped(team_run.id):
                    break
                if rounds >= configuration.max_runs:
                    self._team_run_crud.update_status_if_in(team_run.id, AgentTeamRunStatus.FAILED.value,
                                                            (AgentTeamRunStatus.RUNNING.value,), state=state,
                                                            end_reason=AgentTeamRunEndReason.MAX_RUNS_EXCEEDED.value,
                                                            ended=True)
                    log.error(
                        "agent_team_max_runs_exceeded",
                        extra={
                            "msg": "Agent Team 超过最大轮数，已收敛为失败",
                            "data": {"run_id": team_run.id, "max_runs": configuration.max_runs},
                        },
                    )
                    break
                rounds += 1
                row = self._team_run_crud.update_status_if_in(team_run.id, AgentTeamRunStatus.RUNNING.value,
                                                                (AgentTeamRunStatus.RUNNING.value,), state=state)
                if row is None:
                    log.info(
                        "agent_team_start_failed",
                        extra={
                            "msg": "Agent Team 已被取消",
                            "data": {"run_id": team_run.id},
                        },
                    )
                    break

                node_execution, node_run = self._start_node(
                    team_run,
                    state,
                    configuration,
                    configuration.max_runs - rounds + 1,
                    event_loop,
                    next_node_input,
                )
                if self._stopped(team_run.id):
                    break
                if node_run.status != ConversationRunStatus.COMPLETED.value:
                    failure_reason = (
                        AgentTeamRunEndReason.NODE_RUN_CANCELLED.value
                        if node_run.status == ConversationRunStatus.CANCELLED.value
                        else AgentTeamRunEndReason.NODE_RUN_FAILED.value
                    )
                    log.info(
                        "agent_team_start_failed",
                        extra={
                            "msg": "Agent Team 节点执行失败",
                            "data": {
                                "run_id": team_run.id,
                                "node_id": state.current_node_id,
                                "node_run_id": node_run.id,
                                "node_status": node_run.status,
                                "failure_code": failure_reason,
                            },
                        },
                    )
                    self._team_run_crud.update_status_if_in(
                        team_run.id, AgentTeamRunStatus.FAILED.value,
                        (AgentTeamRunStatus.RUNNING.value,), state=state,
                        end_reason=failure_reason, ended=True,
                    )
                    break

                final_output = node_run.final_output
                parsed_output = self.struct_out_put(final_output)
                if parsed_output is None:
                    log.error(
                        "agent_team_node_output_invalid",
                        extra={"msg": "节点未返回有效的 Team 状态与输出", "data": {
                            "run_id": team_run.id,
                            "node_id": state.current_node_id,
                            "node_run_id": node_run.id,
                        }},
                    )
                    self._team_run_crud.update_status_if_in(
                        team_run.id, AgentTeamRunStatus.FAILED.value,
                        (AgentTeamRunStatus.RUNNING.value,), state=state,
                        end_reason=AgentTeamRunEndReason.NODE_OUTPUT_INVALID.value, ended=True,
                    )
                    break
                status, next_node_input = parsed_output
                next_node_id = state.has_next_node(status)
                if next_node_id is None:
                    log.error(
                        "agent_team_start_failed",
                        extra={
                            "msg": "节点业务状态没有匹配的转移",
                            "data": {
                                "run_id": team_run.id,
                                "node_id": state.current_node_id,
                                "node_run_id": node_run.id,
                                "business_status": status,
                                "failure_code": AgentTeamRunEndReason.TRANSITION_NOT_FOUND.value,
                            },
                        },
                    )
                    self._team_run_crud.update_status_if_in(
                        team_run.id, AgentTeamRunStatus.FAILED.value,
                        (AgentTeamRunStatus.RUNNING.value,), state=state,
                        end_reason=AgentTeamRunEndReason.TRANSITION_NOT_FOUND.value,
                        ended=True,
                    )
                    break
                state.add_transition(state.current_node_id, status, next_node_id)
                if next_node_id == "END":
                    log.info(
                        "agent_team_start_completed",
                        extra={
                            "msg": "Agent Team 已完成",
                            "data": {
                                "run_id": team_run.id,
                                "node_id": state.current_node_id,
                                "output": final_output,
                            },
                        },
                    )
                    final_node_output = next_node_input
                    self._team_run_crud.update_status_if_in(team_run.id, AgentTeamRunStatus.COMPLETED.value,
                                                            (AgentTeamRunStatus.RUNNING.value,), state=state,
                                                            end_reason=final_node_output, ended=True)
                    break
                state.current_node_id = next_node_id
        except Exception as exc:
            failure_reason = (
                AgentTeamRunEndReason.NEXT_NODE_START_FAILED.value
                if state is not None and state.current_node_id is not None
                else AgentTeamRunEndReason.TEAM_START_FAILED.value
            )
            log.exception(
                "agent_team_start_failed",
                extra={
                    "msg": "Agent Team 节点调度异常",
                    "data": {
                        "run_id": team_run.id,
                        "node_id": state.current_node_id if state is not None else None,
                        "failure_code": failure_reason,
                        "error_type": type(exc).__name__,
                    },
                },
            )
            # 原始异常保留在结构化日志，持久化只写稳定原因码供 UI 安全分类。
            self._team_run_crud.update_status_if_in(
                team_run.id, AgentTeamRunStatus.FAILED.value,
                (AgentTeamRunStatus.RUNNING.value,), state=state,
                end_reason=failure_reason, ended=True,
            )
            raise
        finally:
            # 所有正常终态分支与调度异常共用同一档案出口；写入失败由档案 service 记录，
            # 不覆盖已经收敛的 TeamRun 结果。
            self._run_archive_service.write_terminal_archive(team_run.id)
        return self._team_run_crud.get_by_id(team_run.id)

    def struct_out_put(self, output: str | None) -> tuple[str, str] | None:
        """读取节点交接结果；格式错误由调用方记为稳定的节点输出失败。"""

        try:
            json_output = json.loads(output) if output is not None else None
        except json.JSONDecodeError:
            return None
        if not isinstance(json_output, dict):
            return None
        status = json_output.get("status")
        node_output = json_output.get("output")
        if isinstance(status, str) and status and isinstance(node_output, str):
            return status, node_output
        return None

    def _start_node(
            self,
            row: AgentTeamRunModel,
            state: AgentTeamRunState,
            configuration: AgentTeamConfiguration,
            remaining_rounds: int,
            event_loop: asyncio.AbstractEventLoop,
            node_input: str | None = None,
    ) -> tuple[AgentTeamNodeExecution, ConversationRunRecord]:
        """复用或创建节点 Task，跑完本次节点 Run，并返回它落定的执行记录。

        本方法是「可信调用方」的写助手：``row`` / ``state`` / ``configuration`` 由调用方在
        自身事务中加载并校验——``start`` 在入口统一校验 Team 存在、处于 running 且无活动节点；
        驱动循环在结算时按锁定后的快照校验。本方法不再读取或重新校验 Team 身份与生命周期
        状态，只负责按节点快照派生 Task、校验节点 profile、经 ``WorkflowSession.submit`` 把节点
        输入交给节点 Task（静止的节点 Task 随即开 Run），按 Task 等它静止（返回时最新 Run 已是
        终态），再落定执行记录。
        ``state.start_node`` 仍持有活动节点游标的领域守卫。

        同一 TeamRun 中的节点按 ``node_id`` 绑定独立 Task；再次访问该节点时读取原 Task，
        由 Task 级 context 保留它自己的历史。

        前置契约（缺失即异常上浮，由 :meth:`start` 的兜底分支收敛）：``state.node_runtime``
        必然为该 ``node_id`` 备好快照，注册表也必然解析得出该节点的 child profile——两者都在
        确认阶段校验过；这里不重复校验，也不为「取不到」写死降级分支。

        参数:
            row: 已确认的 TeamRun 记录（非 ``pending``）；其 ``id`` 在本方法内被固化使用。
            state: TeamRun 执行快照；``current_node_id`` 指明本次要跑的节点。
            configuration: 本次运行冻结的 Team 配置。
            remaining_rounds: 含本次在内的共享剩余执行预算，用于给节点追加收敛提醒。
            node_input: 上一节点的交接输出；入口节点为 ``None``。

        返回:
            该节点的执行记录（含绑定 Task 与本次 Run ID）与执行后的 Run 记录。

        异常:
            pydantic.ValidationError: 快照中的结构化输出契约非法（建 Run 之前抛出）。
            其余异常（工具 schema 解析失败、注册表未命中等）直接上浮，由 :meth:`start` 统一
            收敛为 TeamRun ``failed``。节点 Run 执行期的失败（含模型配置已删除）由运行时落定
            为该 Run 的 ``failed``，不在这里抛出。

        副作用:
            可能新建节点 Task（首次执行该节点时）；新开一条 running 节点 Run 并执行到终态；
            条件更新 TeamRun 快照与 ``updated_at``；Team 已被取消时停止刚启动的节点 Run。
        """
        node_id = state.current_node_id
        # ``row`` 在下方被条件更新的返回值覆盖；取消分支需要原始 TeamRun 标识，故先固化。
        team_run_id = row.id
        node: TeamNodeDefinition = configuration.node(node_id)
        node_runtime: AgentTeamRunRuntime = state.node_runtime.get(node_id)
        agent_id = node.agent_id
        tool_schemas = self._resolve_node_tool_schemas(node_runtime.allowed_tools)
        node_execution: AgentTeamNodeExecution | None = state.node_execution_for_node(node_id)
        node_goal = node_runtime.node_goal
        input_text = self._build_node_input(
            node_goal=node_goal,
            remaining_rounds=remaining_rounds,
            max_rounds=configuration.max_runs,
            node_input=node_input,
        )
        model_settings = node_runtime.model_settings
        reasoning_effort = model_settings.get("reasoning_effort")
        workspace_root = self._workspace_root(row.workspace_id)
        agent_profile: AgentProfile = self._agent_registry.resolve(workspace_root, agent_id)

        if node_execution is None:
            task = self._task_service.get_or_create_task(
                workspace_id=row.workspace_id,
                title=node.name,
                task_type="agent_team_node",
                parent_task_id=row.parent_task_id,
                extra={
                    "agent_team_run_id": row.id,
                    "agent_team_node_id": node_id,
                    NODE_PROFILE_SNAPSHOT_KEY: node_runtime.model_dump(mode="json"),
                },
                tool_schemas=tool_schemas,
            )
            node_execution: AgentTeamNodeExecution = state.create_execution_node(node_id,
                                                                                 AgentTeamNodeExecution(node_id=node_id,
                                                                                                        task_id=task.id))

        # 节点快照在建 Run 之前先合成一次：契约非法时直接上浮，不留下无执行者的节点 Run。真正驱动
        # 时由 run_driver 从节点 Task 的快照重新合成，不经调度入口在内存中传递定义。
        build_node_profile(agent_profile, node_runtime)
        session = get_workflow_session()
        asyncio.run_coroutine_threadsafe(
            session.submit(
                node_execution.task_id,
                ConversationRunCommand(display_text=input_text),
                agent_id=agent_id,
                model_config_id=node_runtime.model_config_id,
                reasoning_effort=reasoning_effort,
            ),
            event_loop,
        ).result()
        # 节点 Task 只由本协调器顺序投递、提交时必然静止，提交后 Task 的最新 Run 即本次节点 Run。
        node_execution.conversation_run_ids.append(
            self._task_service.get_task(node_execution.task_id).current_run_id
        )
        row = self._team_run_crud.update_status_if_in(
            team_run_id,
            AgentTeamRunStatus.RUNNING.value,
            (AgentTeamRunStatus.RUNNING.value,),
            state=state,
        )
        if row is None:
            # Team 在节点启动期间被取消：停止刚启动的节点 Run，仍等它落定后返回。
            log.info(
                "agent_team_start_failed",
                extra={
                    "msg": "Agent Team 已被取消",
                    "data": {"run_id": team_run_id, "node_id": node_id},
                },
            )
            asyncio.run_coroutine_threadsafe(
                session.cancel(node_execution.task_id, reason="parent"), event_loop
            ).result()
        self._wait_node(event_loop, team_run_id, node_execution.task_id)
        latest = self._task_service.get_task(node_execution.task_id).current_run_id
        return node_execution, self._run_service.get_run(latest)

    def _wait_node(
            self,
            event_loop: asyncio.AbstractEventLoop,
            team_run_id: int,
            node_task_id: int,
    ) -> None:
        """按 Task 阻塞等待节点静止（挂起不算）；期间 TeamRun 被取消则停止节点。"""

        session = get_workflow_session()
        cancel_sent = False
        while not asyncio.run_coroutine_threadsafe(
            session.wait_idle(node_task_id, _NODE_CANCEL_POLL_SECONDS), event_loop
        ).result():
            if not cancel_sent and self._stopped(team_run_id):
                asyncio.run_coroutine_threadsafe(
                    session.cancel(node_task_id, reason="parent"), event_loop
                ).result()
                cancel_sent = True

    def _stopped(self, team_run_id: int) -> bool:
        """TeamRun 已不在运行（被取消或已收敛）时返回真；调度循环与节点等待据此停下。"""

        status = self._team_run_crud.get_by_id(team_run_id).status
        if status == AgentTeamRunStatus.RUNNING.value:
            return False
        log.info(
            "agent_team_stopped",
            extra={
                "msg": "TeamRun 已不在运行，调度停止",
                "data": {"team_run_id": team_run_id, "status": status},
            },
        )
        return True

    def _resolve_node_tool_schemas(self, allowed_tools: list[str]) -> list[dict[str, object]]:
        """按节点快照声明的 ``allowed_tools`` 名字，从当前工具注册表解析冻结 schema。

        节点运行快照只持久化工具名（``allowed_tools``），工具 schema 在节点启动时按名字
        从进程级 ToolSystem 实时解析并固化进 Task。这样快照不必携带易漂移的 schema 契约，
        也保证节点工具集合与确认时一致（工具名冻结于快照）。

        参数:
            allowed_tools: 节点允许使用的工具名列表。

        返回:
            与运行期模型 schema 同源的 JSON 字典列表，可直接传给
            ``TaskService.get_or_create_task`` 的 ``tool_schemas``。

        异常:
            ValueError: 某个工具名在当前注册表中不存在（可能已在确认后从 workspace 移除）。
        """
        registered = {tool.name: tool for tool in self._tool_system.executor.list_tools()}
        schemas: list[dict[str, object]] = []
        for name in allowed_tools:
            tool = registered.get(name)
            if tool is None:
                raise ValueError(
                    f"Team 节点声明的工具 '{name}' 不在当前工具注册表中，"
                    f"无法解析其 schema（可能已在确认后从 workspace 移除）"
                )
            schemas.append(copy.deepcopy(tool.to_model_tool_definition()))
        return schemas

    @staticmethod
    def _build_node_input(
            node_goal: str,
            remaining_rounds: int,
            max_rounds: int,
            node_input: str | None = None,
    ) -> str:
        """组装节点输入，并按剩余执行预算递进提醒节点收敛工作。

        剩余轮次是整个 Team 共享的节点执行预算，并包含当前即将启动的节点执行。
        提示只在剩余预算首次到达 50%、30%、10% 阈值时发送一次；离散额度取第一个不高于
        阈值的整数轮数。最后一次执行机会单独说明，避免把小额度下的最后一轮误报为约一成。
        """

        sections = []
        if node_goal:
            sections.append(f"Current node subgoal:\n{node_goal}")
        if node_input:
            sections.append("Previous node output:\n" + node_input)
        ten_percent_round = max(1, int(max_rounds * 0.1))
        thirty_percent_round = max(1, int(max_rounds * 0.3))
        half_round = max(1, int(max_rounds * 0.5))
        if remaining_rounds == 1:
            sections.append(
                f"Team budget reminder: This is the Agent Team's final node execution ({remaining_rounds}/{max_rounds} remaining, "
                "including the current node). Complete the highest-value work this node can contribute. If the overall goal "
                "is met and this node has an allowed route that ends the Team, choose that route. Otherwise, use the allowed "
                "route that best advances the goal, make the final handoff, and clearly state any unfinished work. No execution "
                "budget will remain after this run."
            )
        elif remaining_rounds == ten_percent_round:
            sections.append(
                f"Team budget reminder: The shared node-execution budget for the entire Agent Team is at or below 10% "
                f"({remaining_rounds}/{max_rounds} remaining, including the current node). Focus on the highest-priority "
                "outcome for the overall goal. Avoid expanding scope or repeating cycles; if handing off, state the conclusions "
                "and remaining work clearly."
            )
        elif remaining_rounds == thirty_percent_round:
            sections.append(
                f"Team budget reminder: The shared node-execution budget for the entire Agent Team is at or below 30% "
                f"({remaining_rounds}/{max_rounds} remaining, including the current node). Narrow the scope and prioritize "
                "work essential to the overall goal. Avoid repeated exploration; for handoffs, clearly state conclusions, "
                "next steps, and blockers."
            )
        elif remaining_rounds == half_round:
            sections.append(
                f"Team budget reminder: At most half of the shared node-execution budget for the entire Agent Team remains "
                f"({remaining_rounds}/{max_rounds}, including the current node). Prioritize the critical path toward the "
                "overall goal, avoid redundant work and unnecessary cycles, and preserve conclusions needed by downstream "
                "nodes when handing off."
            )
        return "\n\n".join(sections) or "Advance the Team's overall goal according to the current node's responsibilities."

    def _workspace_root(self, workspace_id: int) -> str:
        """读取 workspace 根路径。"""

        return self._workspace_service.get_workspace(workspace_id).root_path

    def cancel(self, team_run_id: int) -> AgentTeamRunModel | None:

        row = self._team_run_crud.get_by_id(team_run_id)
        if row.status not in {"pending", "running"}:
            return row
        state = AgentTeamRunState.model_validate(row.state_json)

        row = self._team_run_crud.update_status_if_in(row.id, AgentTeamRunStatus.CANCELLED.value,
                                                      (
                                                          AgentTeamRunStatus.RUNNING.value,
                                                          AgentTeamRunStatus.PENDING.value,
                                                      ),
                                                      end_reason=AgentTeamRunEndReason.CANCELLED.value,
                                                      ended=True)
        if row is None:
            return row

        state.cancel()
        self._run_archive_service.write_terminal_archive(row.id)
        log.info(
            "agent_team_run_cancelled",
            extra={
                "msg": "Agent Team 运行已取消",
                "data": {
                    "team_run_id": row.id,
                    "parent_run_id": row.parent_run_id,
                    "end_reason": AgentTeamRunEndReason.CANCELLED.value,
                },
            },
        )
        return row

    def cancel_for_task_ids(self, task_ids: set[int]) -> int:
        """在删除任务树前取消与其关联的活动 Team。

        参数:
            task_ids: 即将删除的任务及其子任务标识集合。

        返回:
            实际从活动态收敛为 ``cancelled`` 的 Team 数量。

        异常:
            KeyError: TeamRun 在扫描后被其他路径删除时向上暴露，调用方应中止删除；
            数据库或执行器异常：向上暴露，避免删除任务后留下仍在执行的节点。

        副作用:
            取消以这些任务作为主 Agent 的 Team，以及节点 Task 位于集合中的 Team；
            同步发出节点取消信号。
        """

        if not task_ids:
            return 0
        cancelled = 0
        for row in (
                *self._team_run_crud.list_pending_confirmations(),
                *self._team_run_crud.list_active(),
        ):
            node_task_ids = {
                task_id
                for task_id, _ in AgentTeamRunState.model_validate(row.state_json).node_references()
            }
            if row.parent_task_id not in task_ids and not node_task_ids & task_ids:
                continue
            team = self.cancel(row.id)
            if team is not None:
                cancelled += 1
        return cancelled

    def cancel_for_parent_run_id(self, parent_run_id: int) -> int:
        """取消指定主 Agent Run 创建的所有活动 Team。

        该方法由 ``WorkflowSession`` 在主 Run 落定 cancelled 后调用，负责把主 Run 的取消信号
        传播到待确认或运行中的 Team 及其当前节点 Run。

        参数:
            parent_run_id: 主 Agent ConversationRun 标识。

        返回:
            实际从活动态收敛为 ``cancelled`` 的 Team 数量。

        副作用:
            更新 TeamRun 终态并取消节点执行器；异常向调用方暴露，供
            取消入口记录诊断日志，但不回滚主 Run 已发出的取消信号。
        """

        cancelled = 0
        for row in (
                *self._team_run_crud.list_pending_confirmations(),
                *self._team_run_crud.list_active(),
        ):
            if row.parent_run_id != parent_run_id:
                continue
            team = self.cancel(row.id)
            if team is not None:
                cancelled += 1
        return cancelled

    def recover_after_restart(self) -> int:
        """把后端重启遗留的运行中 Team 收敛为 cancelled，不自动重放。

        待确认（pending）的 TeamRun 保留：它挂在仍处于等待态的主 Run 上，重启后用户照常批准、
        驳回或取消；收敛它会让仍可点击的审批卡片必然失败。
        """

        recovered = 0
        for row in self._team_run_crud.list_active():
            previous_status = row.status
            updated = self._team_run_crud.update_status_if_in(
                row.id,
                AgentTeamRunStatus.CANCELLED.value,
                (previous_status,),
                end_reason=AgentTeamRunEndReason.RUNTIME_RESTARTED.value,
                ended=True,
            )
            if updated is None:
                continue
            recovered += 1
            self._run_archive_service.write_terminal_archive(updated.id)
        return recovered

    def get(self, team_run_id: int) -> AgentTeamRunModel:
        """读取 TeamRun 当前持久化快照。"""

        return self._team_run_crud.get_by_id(team_run_id)


_COORDINATOR: AgentTeamCoordinator | None = None


def get_agent_team_coordinator() -> AgentTeamCoordinator:
    """返回进程级 Team coordinator。"""

    global _COORDINATOR
    if _COORDINATOR is None:
        _COORDINATOR = AgentTeamCoordinator()
    return _COORDINATOR
