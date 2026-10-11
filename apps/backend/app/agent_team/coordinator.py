"""Agent Team 节点调度、结果提交和终态收敛。"""

from __future__ import annotations

import asyncio
import copy
import json
from collections import defaultdict
from dataclasses import replace
from threading import RLock
from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.configuration.team_node_definition import TeamNodeDefinition
from app.agent_team.state.agent_team_run_state import AgentTeamRunState, AgentTeamNodeExecution, AgentTeamRunRuntime
from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.model_settings import ModelSettings
from app.core.agents.structured_output_spec import StructuredOutputSpec
from app.models import ConversationRunRecord
from app.models.conversation_run_command import ConversationRunCommand
from app.models.conversation_run_failure import run_failure_message
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service.depends import get_runtime, get_task_service, get_conversation_run_service, \
    get_conversation_run_state_service, get_workspace_service
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.storage.store_engines import main_session_factory


class AgentTeamCoordinator:
    """拥有 TeamRun 的节点调度状态机，并把单节点执行委托给 ConversationRunExecutor。

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
        self._run_state_service = get_conversation_run_state_service()
        self._session_factory = main_session_factory()
        self._locks_guard = RLock()
        self._locks: dict[int, RLock] = defaultdict(RLock)
        self._runtime_loops_guard = RLock()
        self._runtime_loops: dict[int, asyncio.AbstractEventLoop] = {}
        self._runner = get_runtime()
        self._workspace_service = get_workspace_service()
        self._agent_registry = get_agent_registry()

    def start(
            self,
            team_run: AgentTeamRunModel,
    ) -> AgentTeamRunModel:
        """启动已确认的 TeamRun，并登记由 Coordinator 拥有的节点驱动任务。

        本方法只接受已经持久化且完成确认的 ``running`` TeamRun，不读取待确认请求、预览
        文档或用户确认参数。TeamRun 的创建、确认和 ``pending`` 到 ``running`` 的原子迁移
        由执行请求 service 完成；本方法持久化入口节点后，启动一个异步任务串行驱动整条 Team。

        返回:
            已创建入口节点后的 TeamRun 快照；后续节点由异步驱动任务继续调度。

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
                    next_node_input,
                )
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
            node_input: str | None = None,
    ) -> tuple[AgentTeamNodeExecution, ConversationRunRecord]:
        """复用或创建节点 Task，跑完本次节点 Run，并返回它落定的执行记录。

        本方法是「可信调用方」的写助手：``row`` / ``state`` / ``configuration`` 由调用方在
        自身事务中加载并校验——``start`` 在入口统一校验 Team 存在、处于 running 且无活动节点；
        驱动循环在结算时按锁定后的快照校验。本方法不再读取或重新校验 Team 身份与生命周期
        状态，只负责按节点快照派生 Task/Run、合成执行用 per-run profile、**同步执行**该节点
        Run（``asyncio.run``，返回时已是终态）并落定执行记录。节点 Run 先以
        ``pending`` 创建，再通过状态服务认领为 ``running``，使数据库迁移与 Transport 投影共用
        同一事件链路。``state.start_node`` 仍持有活动节点游标的领域守卫。

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
            KeyError: 本次 Run 引用的模型配置已被删除（物化时由配置服务抛出）。
            pydantic.ValidationError: 快照中的结构化输出契约非法。
            RuntimeError: 新创建的节点 Run 未能从 ``pending`` 状态认领。
            其余异常（工具 schema 解析失败、注册表未命中、Run 执行异常等）直接上浮，由
            :meth:`start` 统一收敛为 TeamRun ``failed``。模型配置缺失或结构化输出契约非法时，
            会先把节点 Run 收敛为 ``failed``，避免留下无人认领的 ``running`` 节点 Run。

        副作用:
            可能新建节点 Task（首次执行该节点时）；创建一条 ``pending`` 节点 Run 并原子认领为
            ``running``，再执行到终态；
            profile 合成失败时收敛该 Run 并记 ``agent_team_node_profile_failed`` error 日志；
            条件更新 TeamRun 快照与 ``updated_at``。
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
                parent_run_id=row.parent_run_id,
                extra={
                    "agent_team_run_id": row.id,
                    "agent_team_node_id": node_id,
                    "agent_team_profile_snapshot": node_runtime.model_dump(mode="json"),
                },
                tool_schemas=tool_schemas,
            )
            node_execution: AgentTeamNodeExecution = state.create_execution_node(node_id,
                                                                                 AgentTeamNodeExecution(node_id=node_id,
                                                                                                        task_id=task.id))

        node_run = self._run_service.create_run(
            task_id=node_execution.task_id,
            agent_id=agent_id,
            status=ConversationRunStatus.PENDING.value,
            model_config_id=node_runtime.model_config_id,
            reasoning_effort=reasoning_effort,
            run_command=ConversationRunCommand(display_text=input_text)
        )
        node_execution.conversation_run_ids.append(node_run.id)
        claimed_run = self._run_state_service.claim_pending_run(node_run.id)
        if claimed_run is None:
            raise RuntimeError(f"新创建的节点 Run {node_run.id} 未能从 pending 状态认领")
        node_run = claimed_run
        try:
            run_profile = self._build_node_run_profile(agent_profile, node_runtime, node_run)
        except Exception as exc:
            # 合成失败发生在节点 Run 已落库之后：必须先把这条 running 记录收敛为失败，否则它会成为
            # 无人认领的孤儿（TeamRun 已 failed，节点 Run 永远停在 running、UI 一直转圈）。
            log.exception(
                "agent_team_node_profile_failed",
                extra={
                    "msg": "节点执行 profile 合成失败，已把该节点 Run 收敛为失败",
                    "data": {
                        "run_id": team_run_id,
                        "node_id": node_id,
                        "node_run_id": node_run.id,
                        "error_type": type(exc).__name__,
                    },
                },
            )
            self._run_state_service.fail_run_if_running(
                node_run.id,
                end_reason=Constant.Run.RUN_FAILURE_CODE_UNKNOWN,
                final_output=run_failure_message(Constant.Run.RUN_FAILURE_CODE_UNKNOWN),
            )
            raise

        row = self._team_run_crud.update_status_if_in(team_run_id, AgentTeamRunStatus.RUNNING.value,
                                                (AgentTeamRunStatus.RUNNING.value,), state=state)
        if row is None:
            log.info(
                "agent_team_start_failed",
                extra={
                    "msg": "Agent Team 已被取消",
                    "data": {"run_id": team_run_id, "node_id": node_id},
                },
            )
            return node_execution, node_run


        asyncio.run(self._runner.run_agent(run_profile))
        node_run = self._run_service.get_run(node_run.id)

        return node_execution, node_run

    def _build_node_run_profile(
            self,
            agent_profile: AgentProfile,
            node_runtime: AgentTeamRunRuntime,
            node_run: ConversationRunRecord,
    ) -> AgentProfile:
        """把「注册表节点身份」「确认时冻结的节点快照」与「本次节点 Run」合成执行用 per-run profile。

        字段来源刻意分三路，不要归并：
        - 身份类（``agent_id`` / ``agent_type`` / ``max_steps``）来自注册表 profile；
        - 执行资源（提示词、工具名、结构化输出契约、用户模型偏好）来自节点快照，保证节点行为与
          用户确认时预览的完全一致；
        - 连接与能力字段**只**由本 Run 的 ``model_config_id`` 物化而来：``api_key`` 按契约不落盘，
          快照里若残留连接字段一律经 ``ModelSettings.from_dict`` 忽略，注册表单例（内置子 Agent）
          也只有偏好项——任一者被当作连接来源，模型解析都会以「运行配置未物化」失败。

        参数:
            agent_profile: 注册表解析出的节点 child profile（共享单例，本方法不修改它）。
            node_runtime: 该节点的冻结运行快照（``agent_team_runs.state_json`` 内形态）。
            node_run: 本次节点 Run 记录；其 ``model_config_id`` 是连接字段的唯一来源。

        返回:
            绑定本次 Run 的独立 profile 副本；共享注册表单例与快照都不被修改。

        异常:
            KeyError: ``node_run.model_config_id`` 指向的模型配置已不存在（物化时由配置服务抛出）。
            pydantic.ValidationError: 快照的 ``structured_output`` 不是合法的输出契约。

        副作用:
            只读模型配置；不写状态。``allowed_tools`` 另复制一份列表，避免下游改动回写共享单例。
        """
        node_profile = replace(
            agent_profile,
            role=node_runtime.role or agent_profile.role,
            system_prompt=node_runtime.system_prompt,
            allowed_tools=list(node_runtime.allowed_tools),
            model_settings=ModelSettings.from_dict(node_runtime.model_settings),
            structured_output=(
                StructuredOutputSpec.model_validate(node_runtime.structured_output)
                if node_runtime.structured_output
                else None
            ),
        )
        return node_profile.derive_for_run(
            node_run,
            model_settings=self._runner.materialize_run_model_settings(node_run.model_config_id),
        )

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
            team = self.cancel(row.id)
            if team is not None:
                cancelled += 1
        return cancelled

    def cancel_for_parent_run_id(self, parent_run_id: int) -> int:
        """取消指定主 Agent Run 创建的所有活动 Team。

        该方法由 ConversationRunExecutor 的显式取消入口调用，负责把主 Run 的取消信号
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
        """把后端重启遗留的 Team 运行和未确认方案收敛为 cancelled，不自动重放。"""

        pending = self._team_run_crud.list_pending_confirmations()
        active = self._team_run_crud.list_active()
        recovered = 0
        for row in (*pending, *active):
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
