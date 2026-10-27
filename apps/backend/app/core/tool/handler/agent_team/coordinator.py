"""Agent Team 节点调度、结果提交和终态收敛。"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from typing import Any

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.tool.handler.agent_team.agent_team_run_archive_service import (
    AgentTeamRunArchiveService,
)
from app.core.tool.handler.agent_team.node_profile import (
    NODE_PROFILE_SNAPSHOT_KEY,
    build_node_profile,
)
from app.models import ConversationRunRecord
from app.models.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.models.agent_team.configuration.team_node_definition import TeamNodeDefinition
from app.models.agent_team.state.agent_team_run_state import (
    AgentTeamNodeExecution,
    AgentTeamRunRuntime,
    AgentTeamRunState,
)
from app.models.conversation_run_command import ConversationRunCommand
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service.depends import (
    get_conversation_run_ledger,
    get_task_service,
    get_user_input_service,
    get_workspace_service,
)
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.storage.store_engines import main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

# 等节点静止期间检查 TeamRun 状态、以及等节点开出新 Run 的轮询间隔；与工具执行层的取消轮询同量级。
_NODE_POLL_SECONDS = 0.2
# 投递节点输入后等节点 actor 开出新 Run 的上限。
_NODE_RUN_START_TIMEOUT_SECONDS = 30.0
_ACTIVE_STATUSES = (AgentTeamRunStatus.RUNNING.value, AgentTeamRunStatus.PENDING.value)


class AgentTeamCoordinator:
    """拥有 TeamRun 的节点调度状态机，节点输入经 ``task_runtime_spaces.enqueue`` 投递给节点 Task。

    ``start`` 在 ``agent_team`` 工具的执行线程里同步串行驱动节点：投递输入、等节点 Task 静止、
    读取节点 Run 持久化的 ``final_output``、结算 transition 再进入下一节点，直到 Team 落定终态。
    TeamRun 的创建、确认与驳回归审批门（``agent_team_run_approval``），这里只接已确认的 running
    TeamRun；取消以 TeamRun 状态为唯一事实源（见 :meth:`start`）。
    """

    def __init__(self) -> None:

        from app.config.configuration import get_agent_registry, get_tool_registry

        self._team_run_crud = AgentTeamRunCrud()
        self._task_service = get_task_service()
        self._tool_registry = get_tool_registry()
        self._ledger = get_conversation_run_ledger()
        self._session_factory = main_session_factory()
        self._workspace_service = get_workspace_service()
        self._agent_registry = get_agent_registry()
        self._run_archive_service = AgentTeamRunArchiveService()

    def start(
            self,
            team_run: AgentTeamRunModel,
            event_loop: asyncio.AbstractEventLoop,
    ) -> AgentTeamRunModel:
        """在调用方线程内串行驱动整条已确认的 TeamRun，直到 Team 落定终态。

        ``team_run`` 是 ``agent_team`` 工具刚由审批门迁移为 ``running`` 的行，本方法不再校验其
        状态。调用方是工具的执行线程（阻塞到落定）；节点输入经 ``task_runtime_spaces.enqueue``
        投递，本线程经主事件循环 ``event_loop`` 等节点 actor 静止（``wait_idle``），期间记下新 Run
        并在 TeamRun 不再运行时停下节点（见 :meth:`_await_node_run`）。

        取消：以 TeamRun 自身状态为准，不读父 Task 的取消信号——单一事实源。主 Task 停止时其 actor
        经 :meth:`cancel_for_parent_run_id`、用户在卡片上经 :meth:`cancel` 把 TeamRun 落定
        cancelled；本循环每轮持久化快照时（条件更新落空）与节点结束后检出，等待节点期间检出则
        停止节点。

        返回:
            落定终态后的 TeamRun 快照。调度异常（含已冻结的节点配置或运行快照无效）不向调用方抛出：
            这里记录堆栈、把仍在运行的 TeamRun 收敛为 failed、停止当前节点并写档案，再返回落定
            快照——调用方只需把结论交给模型，不必再捕获、再记一遍日志。

        异常:
            KeyError: 收尾读取时 TeamRun 已不存在。
        """
        state: AgentTeamRunState | None = None
        try:
            state = AgentTeamRunState.model_validate(team_run.state_json)
            configuration = AgentTeamConfiguration.model_validate(
                team_run.configuration_snapshot_json
            )
            state.current_node_id = configuration.start_node_id
            next_node_input = None
            rounds = 0

            while self._persist_running(team_run.id, state):
                if rounds >= configuration.max_runs:
                    self._fail(
                        team_run.id, state, AgentTeamRunEndReason.MAX_RUNS_EXCEEDED,
                        msg="Agent Team 超过最大轮数，已收敛为失败",
                        data={"max_runs": configuration.max_runs},
                    )
                    break
                rounds += 1
                node_run = self._run_node(
                    team_run,
                    state,
                    configuration,
                    configuration.max_runs - rounds + 1,
                    event_loop,
                    next_node_input,
                )
                # 节点因 Team 取消而停下时不再按节点失败归因。
                if self._stopped(team_run.id):
                    break
                node_data = {"node_id": state.current_node_id, "node_run_id": node_run.id}
                if node_run.status != ConversationRunStatus.COMPLETED.value:
                    self._fail(
                        team_run.id, state,
                        AgentTeamRunEndReason.NODE_RUN_CANCELLED
                        if node_run.status == ConversationRunStatus.CANCELLED.value
                        else AgentTeamRunEndReason.NODE_RUN_FAILED,
                        msg="Agent Team 节点执行未完成",
                        data={**node_data, "node_status": node_run.status},
                    )
                    break
                parsed_output = self._parse_handoff(node_run.final_output)
                if parsed_output is None:
                    self._fail(
                        team_run.id, state, AgentTeamRunEndReason.NODE_OUTPUT_INVALID,
                        msg="节点未返回有效的 Team 状态与输出",
                        data=node_data,
                    )
                    break
                status, next_node_input = parsed_output
                next_node_id = state.has_next_node(status)
                if next_node_id is None:
                    self._fail(
                        team_run.id, state, AgentTeamRunEndReason.TRANSITION_NOT_FOUND,
                        msg="节点业务状态没有匹配的转移",
                        data={**node_data, "business_status": status},
                    )
                    break
                state.add_transition(state.current_node_id, status, next_node_id)
                if next_node_id == "END":
                    # 结论在节点 Run 与运行档案里；``end_reason`` 只承载失败/取消原因码。
                    self._team_run_crud.update_status_if_in(
                        team_run.id, AgentTeamRunStatus.COMPLETED.value,
                        (AgentTeamRunStatus.RUNNING.value,), state=state, ended=True,
                    )
                    log.info(
                        "agent_team_completed",
                        extra={
                            "msg": "Agent Team 已完成",
                            "data": {"team_run_id": team_run.id, **node_data},
                        },
                    )
                    break
                state.current_node_id = next_node_id
        except Exception as exc:
            failure_reason = (
                AgentTeamRunEndReason.NEXT_NODE_START_FAILED.value
                if state is not None and state.current_node_id is not None
                else AgentTeamRunEndReason.TEAM_START_FAILED.value
            )
            log.exception(
                "agent_team_dispatch_failed",
                extra={
                    "msg": "Agent Team 节点调度异常",
                    "data": {
                        "team_run_id": team_run.id,
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
            # 异常可能发生在输入已投递之后（等新 Run 超时、等静止失败）：不停下节点，它会在没有
            # 协调者的情况下继续执行。用内存快照定位——此时节点执行记录可能尚未落库。
            execution = (
                state.node_execution_for_node(state.current_node_id)
                if state is not None and state.current_node_id is not None
                else None
            )
            if execution is not None:
                task_runtime_spaces.cancel(execution.task_id, "parent")
        finally:
            # 所有正常终态分支与调度异常共用同一档案出口；写入失败由档案 service 记录，
            # 不覆盖已经收敛的 TeamRun 结果。
            self._run_archive_service.write_terminal_archive(team_run.id)
        return self._team_run_crud.get_by_id(team_run.id)

    def _persist_running(self, team_run_id: int, state: AgentTeamRunState) -> bool:
        """把调度快照写回仍在运行的 TeamRun；条件更新落空即 Team 已停止，返回假。"""

        row = self._team_run_crud.update_status_if_in(
            team_run_id,
            AgentTeamRunStatus.RUNNING.value,
            (AgentTeamRunStatus.RUNNING.value,),
            state=state,
        )
        if row is not None:
            return True
        log.info(
            "agent_team_stopped",
            extra={
                "msg": "TeamRun 已不在运行，调度停止",
                "data": {"team_run_id": team_run_id, "node_id": state.current_node_id},
            },
        )
        return False

    def _fail(
            self,
            team_run_id: int,
            state: AgentTeamRunState,
            reason: AgentTeamRunEndReason,
            *,
            msg: str,
            data: dict[str, Any],
    ) -> None:
        """把运行中的 TeamRun 收敛为 failed，并以统一事件记录失败原因码。"""

        self._team_run_crud.update_status_if_in(
            team_run_id, AgentTeamRunStatus.FAILED.value,
            (AgentTeamRunStatus.RUNNING.value,), state=state,
            end_reason=reason.value, ended=True,
        )
        log.warning(
            "agent_team_failed",
            extra={
                "msg": msg,
                "data": {"team_run_id": team_run_id, "failure_code": reason.value, **data},
            },
        )

    @staticmethod
    def _parse_handoff(output: str | None) -> tuple[str, str] | None:
        """读取节点交接结果 ``{"status", "output"}``；格式错误由调用方记为节点输出失败。"""

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

    def _run_node(
            self,
            row: AgentTeamRunModel,
            state: AgentTeamRunState,
            configuration: AgentTeamConfiguration,
            remaining_rounds: int,
            event_loop: asyncio.AbstractEventLoop,
            node_input: str | None = None,
    ) -> ConversationRunRecord:
        """复用或创建节点 Task，跑完本次节点 Run，返回它落定的 Run 记录。

        ``row`` / ``state`` / ``configuration`` 由 :meth:`start` 提供并信任，这里不重新校验 Team
        身份与生命周期状态。流程：按节点快照派生 Task、校验节点 profile、经
        ``task_runtime_spaces.enqueue`` 投递节点输入（静止的节点 Task 随即开 Run），等新 Run 出现
        即把它记入快照并落库，再按 Task 等它静止（返回时最新 Run 已是终态）。

        同一 TeamRun 中的节点按 ``node_id`` 绑定独立 Task；再次访问该节点时读取原 Task，
        由 Task 级 context 保留它自己的历史。

        前置契约（缺失即异常上浮，由 :meth:`start` 的兜底分支收敛）：``state.node_runtime``
        必然为该 ``node_id`` 备好快照，注册表也必然解析得出该节点的 child profile——两者都在
        确认阶段校验过；这里不重复校验，也不为「取不到」写死降级分支。

        参数:
            remaining_rounds: 含本次在内的共享剩余执行预算，用于给节点追加收敛提醒。
            node_input: 上一节点的交接输出；入口节点为 ``None``。

        异常:
            pydantic.ValidationError: 快照中的结构化输出契约非法（投递输入之前抛出）。
            RuntimeError: 节点输入已投递，但节点 Run 没能建立（原因由调度 actor 记录）。
            其余异常（工具 schema 解析失败、注册表未命中等）直接上浮。节点 Run 执行期的失败（含模型
            配置已删除）由运行时落定为该 Run 的 ``failed``，不在这里抛出。

        副作用:
            可能新建节点 Task（首次执行该节点时）；新开一条节点 Run 并等它到终态；条件更新 TeamRun
            快照；TeamRun 不再运行时停止节点（见 :meth:`_await_node_run`）。
        """
        node_id = state.current_node_id
        node: TeamNodeDefinition = configuration.node(node_id)
        node_runtime: AgentTeamRunRuntime = state.node_runtime.get(node_id)
        agent_id = node.agent_id
        tool_schemas = self._resolve_node_tool_schemas(node_runtime.allowed_tools)
        node_execution: AgentTeamNodeExecution | None = state.node_execution_for_node(node_id)
        input_text = self._build_node_input(
            node_goal=node_runtime.node_goal,
            remaining_rounds=remaining_rounds,
            max_rounds=configuration.max_runs,
            node_input=node_input,
        )
        workspace_root = self._workspace_service.get_workspace(row.workspace_id).root_path
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
            node_execution = state.create_execution_node(
                node_id, AgentTeamNodeExecution(node_id=node_id, task_id=task.id)
            )

        # 节点快照在投递输入之前先合成一次：契约非法时直接上浮，不留下无执行者的节点 Run。真正
        # 驱动时由 ``AgentRuntimeConfig.for_run`` 从节点 Task 的快照重新合成，不经调度入口在内存中
        # 传递定义。
        build_node_profile(agent_profile, node_runtime)
        previous_run_id = self._task_service.get_task(node_execution.task_id).current_run_id
        message = get_user_input_service().prepare(
            node_execution.task_id,
            ConversationRunCommand(display_text=input_text),
            node_runtime.model_config_id,
            node_runtime.model_settings.get("reasoning_effort"),
            agent_id,
        )
        task_runtime_spaces.enqueue(node_execution.task_id, message)
        node_run_id = self._await_node_run(
            event_loop, row.id, state, node_execution, previous_run_id
        )
        return self._ledger.get_run(node_run_id)

    def _await_node_run(
            self,
            event_loop: asyncio.AbstractEventLoop,
            team_run_id: int,
            state: AgentTeamRunState,
            node_execution: AgentTeamNodeExecution,
            previous_run_id: int | None,
    ) -> int:
        """投递节点输入后阻塞到节点 Run 落定（挂起不算），返回该 Run id；本线程是工作线程。

        本协调器是节点的唯一停止者：TeamRun 的取消入口（:meth:`cancel` 等）只迁移状态，不碰节点；
        这里每 ``_NODE_POLL_SECONDS`` 检查一次 TeamRun，不再运行就停下节点 Task。TeamRun 处于
        running 时 :meth:`start` 必然在执行，所以这条路径不会缺席；单一负责方也就没有「取消入口
        找不到刚启动的节点」的竞态窗口。

        取消只在看到新 Run 之后发：Run 开出来之前节点 actor 没有可停止的对象，发了即落空。新 Run
        一出现即记入快照并落库，只为执行时间线定位。节点 Task 只由本协调器顺序投递、投递时必然
        静止，投递必然新开 Run；超时没开出来说明调度违约，抛 ``RuntimeError`` 由 :meth:`start`
        收敛为 TeamRun ``failed``。
        """

        # TODO(Agent Team 接入)：改为订阅节点 actor 的状态，不轮询 Task 当前 Run 与 TeamRun 状态。
        actor = task_runtime_spaces.get_or_create(node_execution.task_id).actor
        deadline = time.monotonic() + _NODE_RUN_START_TIMEOUT_SECONDS
        node_run_id: int | None = None
        cancel_sent = False
        while True:
            idle = asyncio.run_coroutine_threadsafe(
                actor.wait_idle(_NODE_POLL_SECONDS), event_loop
            ).result()
            if node_run_id is None:
                current = self._task_service.get_task(node_execution.task_id).current_run_id
                if current is None or current == previous_run_id:
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            f"task {node_execution.task_id} 的节点输入没有启动新 Run"
                        )
                    if idle:
                        # 静止时 ``wait_idle`` 立即返回：新 Run 还没开出来，自行让出间隔。
                        time.sleep(_NODE_POLL_SECONDS)
                    continue
                node_run_id = current
                node_execution.conversation_run_ids.append(node_run_id)
                self._persist_running(team_run_id, state)
            if idle:
                return node_run_id
            if not cancel_sent and self._stopped(team_run_id):
                task_runtime_spaces.cancel(node_execution.task_id, "parent")
                cancel_sent = True

    def _stopped(self, team_run_id: int) -> bool:
        """TeamRun 已不在运行（被取消或已收敛）时返回真；节点结束后与节点等待期间据此停下。"""

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
        从进程级工具注册表实时解析并固化进 Task。这样快照不必携带易漂移的 schema 契约，
        也保证节点工具集合与确认时一致（工具名冻结于快照）。

        参数:
            allowed_tools: 节点允许使用的工具名列表。

        返回:
            与运行期模型 schema 同源的 JSON 字典列表，可直接传给
            ``TaskService.get_or_create_task`` 的 ``tool_schemas``。

        异常:
            ValueError: 某个工具名在当前注册表中不存在（可能已在确认后从 workspace 移除）。
        """
        registered = {tool.name: tool for tool in self._tool_registry.get_all_definitions()}
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

    def cancel(
            self,
            team_run_id: int,
            end_reason: AgentTeamRunEndReason = AgentTeamRunEndReason.CANCELLED,
    ) -> AgentTeamRunModel:
        """把待确认或运行中的 TeamRun 落定 cancelled；已在终态时原样返回当前快照。

        只迁移状态：运行中的节点由 :meth:`start` 的等待循环检出后停下。

        ``end_reason`` 区分用户在卡片上取消与随主 Run 停止（``PARENT_CANCELLED``），写进 TeamRun
        与运行档案供诊断。

        异常:
            KeyError: TeamRun 不存在。
        """

        cancelled = self._cancel_if_active(team_run_id, end_reason)
        return cancelled if cancelled is not None else self._team_run_crud.get_by_id(team_run_id)

    def _cancel_if_active(
            self,
            team_run_id: int,
            end_reason: AgentTeamRunEndReason,
    ) -> AgentTeamRunModel | None:
        """条件迁移为 cancelled 并写档案；TeamRun 已不在活动态（含并发落定）时返回 None。

        不先读状态再判断：条件更新本身就是判定，避免读与写之间被其他路径落定。不停止节点：节点由
        :meth:`start` 的等待循环检出 TeamRun 不再运行后停下（见 :meth:`_await_node_run`）；运行档案
        由 :meth:`start` 收尾时再按最终状态重写一次。
        """

        row = self._team_run_crud.update_status_if_in(
            team_run_id,
            AgentTeamRunStatus.CANCELLED.value,
            _ACTIVE_STATUSES,
            end_reason=end_reason.value,
            ended=True,
        )
        if row is None:
            return None
        self._run_archive_service.write_terminal_archive(row.id)
        log.info(
            "agent_team_run_cancelled",
            extra={
                "msg": "Agent Team 运行已取消",
                "data": {
                    "team_run_id": row.id,
                    "parent_run_id": row.parent_run_id,
                    "end_reason": end_reason.value,
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
            数据库异常：向上暴露，调用方应中止删除。

        副作用:
            取消以这些任务作为主 Agent 的 Team，以及节点 Task 位于集合中的 Team。节点 Task 在任务树
            里（``parent_task_id`` 是主 Task），由删除入口先经 ``task_runtime_spaces.stop`` 停下。
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
            if self._cancel_if_active(row.id, AgentTeamRunEndReason.CANCELLED) is not None:
                cancelled += 1
        return cancelled

    def cancel_for_parent_run_id(self, parent_run_id: int) -> int:
        """取消指定主 Agent Run 创建的所有活动 Team。

        由主 Task 的 actor 在发起停止时调用（不等主 Run 落定：``agent_team`` 工具阻塞在 Team 上，
        主 Run 要等 Team 停下才能走到检查点），把停止传播到待确认或运行中的 Team 及其当前节点 Run。

        参数:
            parent_run_id: 主 Agent ConversationRun 标识。

        返回:
            实际从活动态收敛为 ``cancelled`` 的 Team 数量。

        副作用:
            更新 TeamRun 终态（节点由 :meth:`start` 的等待循环停下）；异常向调用方暴露，供
            取消入口记录诊断日志，但不回滚主 Run 已发出的取消信号。
        """

        cancelled = 0
        for row in (
                *self._team_run_crud.list_pending_confirmations(),
                *self._team_run_crud.list_active(),
        ):
            if row.parent_run_id != parent_run_id:
                continue
            if self._cancel_if_active(row.id, AgentTeamRunEndReason.PARENT_CANCELLED) is not None:
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


_COORDINATOR: AgentTeamCoordinator | None = None


def get_agent_team_coordinator() -> AgentTeamCoordinator:
    """返回进程级 Team coordinator。"""

    global _COORDINATOR
    if _COORDINATOR is None:
        _COORDINATOR = AgentTeamCoordinator()
    return _COORDINATOR
