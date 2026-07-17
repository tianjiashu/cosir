"""协调任务生命周期与工作流执行。"""

import asyncio
from datetime import datetime, timezone
import logging
import os
from typing import AsyncIterator, Optional

from app.core.agents.profile import AgentProfile, default_developer_agent
from app.domain.approvals.service import ApprovalService
from app.config.settings import BackendSettings
from app.context.builder import TextContextBuilder
from app.core.logs.query_service import LogQueryService
from app.core.replay.service import ReplayService
from app.core.trace.event_names import runtime_trace_event_name
from app.core.trace.query_service import TraceQueryService
from app.core.trace.recorder import TraceRecorder
from app.events.types import EventType, RuntimeEvent
from app.config.logging import bind_log_context, reset_log_context, set_log_context, trace_log_extra
from app.core.runs.langgraph_runtime import LangGraphRuntime
from app.core.runs.recovery import RecoveryManager
from app.core.runs.resume import ResumeDispatcher
from app.storage.crud.durable import DurableRunStore
from app.models.base import StreamingModelAdapter
from app.core.runtime.operations import RuntimeOperations
from app.storage.records import CheckpointRecord, TaskRecord, TurnRecord, WorkspaceRecord
from app.tools.runtime.compatibility import ToolScheduler
from app.tools.runtime.platform import ToolExecutionContext, ToolRuntime
from app.tools.schemas import ToolCall
from app.core.workflows.react_like import ReactLikeWorkflow
from app.core.workflows.types import AgentWorkflow



class AgentRuntime:
    """协调任务状态、模型流式产出、事件与日志。"""

    def __init__(
        self,
        settings: BackendSettings,
        task_store,
        context_builder: TextContextBuilder,
        model_adapter: StreamingModelAdapter,
        tool_scheduler: ToolScheduler,
        logger: logging.Logger,
        agent_profile: Optional[AgentProfile] = None,
        workflow: Optional[AgentWorkflow] = None,
        run_store: Optional[DurableRunStore] = None,
        approval_service: Optional[ApprovalService] = None,
        recovery_manager: Optional[RecoveryManager] = None,
        resume_dispatcher: Optional[ResumeDispatcher] = None,
        langgraph_runtime: Optional[LangGraphRuntime] = None,
        tool_runtime: Optional[ToolRuntime] = None,
        trace_recorder: Optional[TraceRecorder] = None,
        trace_query_service: Optional[TraceQueryService] = None,
        replay_service: Optional[ReplayService] = None,
        log_query_service: Optional[LogQueryService] = None,
    ) -> None:
        """初始化运行时依赖项。

        参数:
            settings: 控制运行时限制的后端配置。
            task_store: 用于任务状态与事件的存储实现。
            context_builder: 准备与模型无关的运行时消息的构建器。
            model_adapter: 用于模型调用的流式适配器。
            tool_scheduler: 用于校验和执行工具调用的调度器。
            logger: 用于可审计运行时记录的日志记录器。
            agent_profile: 作为任务执行主体的 Agent 档案。
            workflow: 可选的工作流策略。默认为 ReAct 风格工作流。
            run_store: 可选的 Durable Run State 仓储。
            approval_service: 可选的工具审批服务。
            recovery_manager: 可选的启动恢复对账管理器。
            resume_dispatcher: 可选的恢复命令分发器，用于记录恢复消费 trace。
            langgraph_runtime: 可选的 LangGraph 生命周期持久化运行时。
            tool_runtime: 可选 Tool v2 工具执行入口。
            trace_recorder: 可选 Trace Backbone 写入器。
            trace_query_service: 可选 Trace 查询服务。
            replay_service: 可选 Agent Replay 查询服务。
            log_query_service: 可选日志查询服务。

        返回:
            无。

        异常:
            无。

        副作用:
            在运行时实例上保存依赖项引用。
        """

        self._settings = settings
        self._task_store = task_store
        self._context_builder = context_builder
        self._model_adapter = model_adapter
        self._tool_scheduler = tool_scheduler
        self._logger = logger
        self._agent_profile = agent_profile or default_developer_agent()
        self._workflow = workflow or ReactLikeWorkflow()
        self._run_store = run_store
        self._approval_service = approval_service
        self._recovery_manager = recovery_manager
        self._resume_dispatcher = resume_dispatcher
        self._langgraph_runtime = langgraph_runtime
        self._tool_runtime = tool_runtime
        self._trace_recorder = trace_recorder
        self._trace_query_service = trace_query_service
        self._replay_service = replay_service
        self._log_query_service = log_query_service

    def close(self) -> None:
        """关闭运行时持有的外部资源。

        参数:
            无。

        返回:
            无。

        异常:
            无。关闭失败会被记录为错误日志。

        副作用:
            关闭 LangGraph checkpointer 等运行时级资源。
        """

        if self._langgraph_runtime is None:
            return
        try:
            self._langgraph_runtime.close()
        except Exception:
            self._logger.exception(
                "runtime_close_failed",
                extra={"msg": "关闭 LangGraph checkpointer 等运行时资源失败"},
            )

    def create_workspace(self, name: str, root_path: str) -> WorkspaceRecord:
        """创建一个本地工作区。

        参数:
            name: 用户可读的工作区名称。
            root_path: 工作区本地路径。

        返回:
            已创建的工作区记录。

        异常:
            ValueError: 如果名称或路径为空。

        副作用:
            在任务存储中持久化工作区，并写入 info 日志。
        """

        workspace = self._task_store.create_workspace(name, root_path)
        self._logger.info(
            "workspace_created",
            extra={
                "msg": f"工作区已创建，workspace_id={workspace.workspace_id}",
                "data": {"workspace_id": workspace.workspace_id, "root_path": workspace.root_path},
            },
        )
        return workspace

    def list_workspaces(self) -> list[WorkspaceRecord]:
        """列出所有本地工作区。

        参数:
            无。

        返回:
            工作区记录列表。

        异常:
            无。

        副作用:
            无。
        """

        return self._task_store.list_workspaces()

    def delete_workspace(self, workspace_id: str) -> None:
        """删除工作区及其下游任务记录。

        参数:
            workspace_id: 待删除的工作区标识符。

        返回:
            无。

        异常:
            KeyError: 如果工作区不存在。

        副作用:
            级联删除任务运行记录，并写入 info 日志。
        """

        tasks = self._task_store.list_tasks_for_workspace(workspace_id)
        task_ids = [task.task_id for task in tasks]
        self._logger.info(
            "workspace_delete_start",
            extra={
                "msg": f"开始删除工作区及其下游记录，workspace_id={workspace_id}",
                "data": {"workspace_id": workspace_id, "task_count": len(task_ids)},
            },
        )
        run_ids = []
        if self._run_store is not None:
            for task_id in task_ids:
                run_ids.extend(run.run_id for run in self._run_store.list_by_task(task_id))
        if self._approval_service is not None:
            self._approval_service.delete_run_data(run_ids)
        if self._run_store is not None:
            self._run_store.delete_by_task_ids(task_ids)
        if self._trace_recorder is not None:
            self._trace_recorder.delete_task_traces(task_ids)
        self._task_store.delete_workspace(workspace_id)
        self._logger.info(
            "workspace_deleted",
            extra={
                "msg": f"工作区及其任务记录已删除，workspace_id={workspace_id}",
                "data": {"workspace_id": workspace_id, "task_ids": task_ids, "run_ids": run_ids},
            },
        )

    def list_workspace_tasks(self, workspace_id: str) -> list[TaskRecord]:
        """列出一个工作区下的任务容器。

        参数:
            workspace_id: 待查询的工作区标识符。

        返回:
            任务记录列表。

        异常:
            KeyError: 如果工作区不存在。

        副作用:
            无。
        """

        return self._task_store.list_tasks_for_workspace(workspace_id)

    def create_task(
        self,
        input_text: str,
        workspace_id: Optional[str] = None,
    ) -> TaskRecord:
        """创建一个待执行的任务，留待后续执行。

        参数:
            input_text: 纯文本的用户任务。
            workspace_id: 可选的工作区标识符；省略时使用默认工作区。

        返回:
            已创建的任务记录。

        异常:
            ValueError: 如果 ``input_text`` 为空。

        副作用:
            在配置好的任务存储中持久化任务状态，并写入一条 info 日志。
        """

        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("input_text must be a non-empty string")
        task = self._task_store.create_task(
            input_text=input_text,
            status="pending",
            agent_id=self._agent_profile.agent_id,
            workspace_id=workspace_id,
        )
        context = self._trace_context_for_task(task.task_id)
        token = set_log_context(context)
        try:
            self._logger.info(
                "task_created",
                extra={
                    "msg": f"新任务已创建，等待调度，task_id={task.task_id}",
                    "data": {
                        "task_id": task.task_id,
                        "workspace_id": task.workspace_id,
                        "agent_id": self._agent_profile.agent_id,
                    },
                },
            )
        finally:
            reset_log_context(token)
        if self._run_store is not None:
            token = set_log_context(context)
            try:
                run = self._run_store.create_for_turn(task.task_id, task.latest_turn_id or task.task_id, "created")
            finally:
                reset_log_context(token)
            context = self._trace_context_for_task(task.task_id, run.run_id)
            if self._trace_recorder is not None:
                self._trace_recorder.record_event(
                    context,
                    "run_created",
                    {"status": run.status, "thread_id": run.thread_id},
                    source="runtime",
                )
            self._logger.info(
                "durable_run_bound",
                extra=trace_log_extra(
                    context,
                    msg=f"任务已绑定 Durable Run，task_id={task.task_id}，run_id={run.run_id}",
                    data={
                        "task_id": task.task_id,
                        "turn_id": run.turn_id,
                        "run_id": run.run_id,
                        "thread_id": run.thread_id,
                    },
                ),
            )
        return task

    def create_turn(self, task_id: str, input_text: str) -> TurnRecord:
        """为已有任务创建一个 pending 轮次。

        参数:
            task_id: 目标任务容器标识符。
            input_text: 本轮用户输入文本。

        返回:
            已创建的轮次记录。

        异常:
            KeyError: 如果任务不存在。
            ValueError: 如果输入为空。

        副作用:
            写入 turns 表并记录 info 日志；不会启动运行。
        """

        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("input_text must be a non-empty string")
        turn = self._task_store.create_turn(task_id, input_text, "pending")
        if self._run_store is not None:
            run = self._run_store.create_for_turn(task_id, turn.turn_id, "created")
            if self._trace_recorder is not None:
                self._trace_recorder.record_event(
                    self._trace_context_for_task(task_id, run.run_id),
                    "run_created",
                    {"status": run.status, "thread_id": run.thread_id, "turn_id": turn.turn_id},
                    source="runtime",
                )
        self._logger.info(
            "turn_created",
            extra=trace_log_extra(
                self._trace_context_for_task(task_id),
                msg=f"新轮次已创建，task_id={task_id}，turn_id={turn.turn_id}",
                data={"task_id": task_id, "turn_id": turn.turn_id},
            ),
        )
        return turn

    def list_turns(self, task_id: str) -> list[TurnRecord]:
        """列出一个任务下的所有轮次。

        参数:
            task_id: 待查询的任务标识符。

        返回:
            轮次记录列表。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        return self._task_store.list_turns_for_task(task_id)

    def get_turn(self, turn_id: str) -> TurnRecord:
        """按标识符返回一个轮次。

        参数:
            turn_id: 待查询的轮次标识符。

        返回:
            匹配的轮次记录。

        异常:
            KeyError: 如果轮次不存在。

        副作用:
            无。
        """

        return self._task_store.get_turn(turn_id)

    def cancel_task(self, task_id: str) -> TaskRecord:
        """通过更新运行时状态来取消一个任务。

        参数:
            task_id: 需要取消的任务标识符。

        返回:
            已更新的任务记录。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            修改任务状态并写入一条可审计的日志记录。
        """

        runs = self._run_store.list_by_task(task_id) if self._run_store is not None else []
        run = runs[-1] if runs else None
        token = set_log_context(self._trace_context_for_task(task_id, run.run_id if run is not None else ""))
        try:
            task = self._task_store.update_status(task_id, "cancelled")
            for item in runs:
                self._mark_run_for_turn(item.turn_id, "cancelled", interruption_reason="task_cancelled")
            self._record(EventType.RUN_CANCELLED, task_id, {"status": "cancelled"})
            self._logger.info(
                "task_cancelled",
                extra={
                    "msg": f"任务已取消，task_id={task_id}",
                    "data": {"task_id": task_id},
                },
            )
            return task
        finally:
            reset_log_context(token)

    def list_pending_approvals(self, task_id: Optional[str] = None) -> list:
        """列出待处理审批请求。

        参数:
            task_id: 可选任务标识符；提供时只查询该任务对应 run。

        返回:
            可序列化的审批请求列表。

        异常:
            RuntimeError: 如果运行时未配置审批服务。
            KeyError: 如果 task_id 不存在或未绑定 run。

        副作用:
            无。
        """

        if self._approval_service is None or self._run_store is None:
            raise RuntimeError("approval service is not configured")
        if task_id is None:
            approvals = self._approval_service.list_pending()
        else:
            self._task_store.get_task(task_id)
            runs = self._run_store.list_by_task(task_id)
            approvals = []
            for run in runs:
                approvals.extend(self._approval_service.list_pending(run.run_id))
        return [approval.to_dict() for approval in approvals]

    def decide_approval(
        self,
        approval_id: str,
        decision: str,
        reason: Optional[str],
        idempotency_key: str,
    ) -> dict:
        """处理审批决策。

        参数:
            approval_id: 审批请求标识符。
            decision: approved 或 denied。
            reason: 可选决策原因。
            idempotency_key: 幂等键。

        返回:
            可序列化的审批决策记录。

        异常:
            RuntimeError: 如果运行时未配置审批服务。
            ValueError: 如果决策值非法。
            KeyError: 如果审批请求不存在。

        副作用:
            写入审批决策和恢复命令，并记录日志。
        """

        if self._approval_service is None:
            raise RuntimeError("approval service is not configured")
        approval = self._approval_service.get_request(approval_id)
        run = self._run_store.get(approval.run_id) if self._run_store is not None else None
        token = set_log_context(self._trace_context_for_task(run.task_id if run is not None else "", approval.run_id))
        try:
            record = self._approval_service.decide(
                approval_id=approval_id,
                decision=decision,
                reason=reason,
                idempotency_key=idempotency_key,
            )
            self._consume_pending_resume_commands(approval.run_id)
            return record.to_dict()
        finally:
            reset_log_context(token)

    def _consume_pending_resume_commands(self, run_id: Optional[str] = None) -> None:
        """领取并执行已持久化的审批恢复命令。

        参数:
            run_id: 可选运行标识；省略时消费全部待处理命令。

        返回:
            无。

        异常:
            无。单个命令失败会回到 pending，留给恢复流程重试。

        副作用:
            仅对原子领取成功的命令恢复工具或 LangGraph，并标记命令已应用。
        """

        if self._run_store is None or self._approval_service is None:
            return
        for command in self._run_store.claim_resume_commands(
            "pending",
            "processing",
            run_id,
            actions=("approve_tool", "deny_tool"),
        ):
            approval_id = command.payload.get("approval_id")
            if command.action not in {"approve_tool", "deny_tool"} or not isinstance(approval_id, str):
                self._logger.error(
                    "resume_command_unsupported",
                    extra={
                        "msg": f"恢复命令类型不受支持，run_id={command.run_id}，action={command.action}",
                        "data": {
                            "run_id": command.run_id,
                            "resume_command_id": command.command_id,
                            "action": command.action,
                        },
                    },
                )
                if self._resume_dispatcher is not None:
                    self._resume_dispatcher.record_failed(command, "unsupported_resume_command")
                self._run_store.update_resume_command_status(command.command_id, "processing", "pending")
                continue
            try:
                if self._resume_dispatcher is not None:
                    self._resume_dispatcher.record_started(command)
                approval = self._approval_service.get_request(approval_id)
                self._resume_approved_tool_call(approval, command.payload.get("decision", ""))
                decision = self._approval_service.get_decision(approval_id)
                if decision is None:
                    raise RuntimeError("approval decision is missing for resume command")
                self._resume_langgraph_for_approval(approval_id, decision.to_dict())
                self._finalize_approval_resume(command.run_id, decision.decision)
                self._run_store.update_resume_command_status(command.command_id, "processing", "applied", applied_at=datetime.now(timezone.utc))
                if self._resume_dispatcher is not None:
                    self._resume_dispatcher.record_completed(command, decision.decision)
            except Exception:
                self._logger.exception(
                    "resume_command_consume_failed",
                    extra={
                        "msg": f"恢复命令消费失败，run_id={command.run_id}，action={command.action}",
                        "data": {
                            "run_id": command.run_id,
                            "resume_command_id": command.command_id,
                            "action": command.action,
                        },
                    },
                )
                if self._resume_dispatcher is not None:
                    self._resume_dispatcher.record_failed(command, "resume_command_consume_failed")
                self._run_store.update_resume_command_status(command.command_id, "processing", "pending")

    def _resume_approved_tool_call(self, approval, decision: str) -> None:
        """消费审批决策并恢复或取消原始持久化工具调用。

        参数:
            approval: 已持久化审批请求。
            decision: 已记录的审批决策。

        返回:
            无。

        异常:
            ValueError: 如果审批载荷缺少原始工具参数。
            Exception: 如果 ToolRuntime 恢复执行失败。

        副作用:
            已批准时可能执行一次原始工具副作用，并追加工具完成事件。
        """

        if self._tool_runtime is None:
            return
        arguments = approval.payload.get("arguments")
        if not isinstance(arguments, dict):
            self._logger.error(
                "tool_resume_payload_invalid",
                extra={
                    "msg": f"审批载荷缺少工具参数，无法恢复工具调用，run_id={approval.run_id}，approval_id={approval.approval_id}",
                    "data": {"run_id": approval.run_id, "approval_id": approval.approval_id},
                },
            )
            raise ValueError("approval payload must include tool arguments")
        observation = self._tool_runtime.resume_approved_tool_call(
            ToolCall(approval.tool_name, arguments),
            ToolExecutionContext(approval.run_id, approval.step_id),
            approval.approval_id,
        )
        run = self._run_store.get(approval.run_id) if self._run_store is not None else None
        if run is not None:
            self._record(
                EventType.TOOL_CALL_FINISHED,
                run.task_id,
                {
                    "tool_name": observation.tool_name,
                    "tool_call_id": observation.tool_call_id or approval.tool_call_id,
                    "status": observation.status,
                    "error": observation.error,
                    "artifact_id": observation.artifact_id,
                },
            )

    def _finalize_approval_resume(self, run_id: str, decision: str) -> None:
        """在当前无 continuation 的工作流中收束审批恢复后的任务与运行状态。

        参数:
            run_id: 已消费审批恢复命令的运行标识符。
            decision: 审批决策，approved 或 denied。

        返回:
            无。

        异常:
            Exception: 如果任务或运行状态收束失败，会抛给恢复命令消费者重试。

        副作用:
            将等待中的任务和 Durable Run 推进到可解释终态，并记录对应运行事件。
        """

        if self._run_store is None:
            return
        try:
            run = self._run_store.get(run_id)
            task = self._task_store.get_task(run.task_id)
            task_can_finish = task.status in {"waiting", "running", "pending"}
            run_can_finish = run.status in {"resuming", "waiting", "running"}
            if not task_can_finish or not run_can_finish:
                return
            if decision == "approved":
                self._run_store.mark_status(run_id, "completed")
                self._task_store.update_status(run.task_id, "completed")
                self._record(
                    EventType.RUN_FINISHED,
                    run.task_id,
                    {"status": "completed", "resume_decision": decision},
                )
            else:
                self._run_store.mark_status(run_id, "failed", interruption_reason="approval_denied")
                self._task_store.update_status(run.task_id, "failed")
                self._record(
                    EventType.RUN_FAILED,
                    run.task_id,
                    {"status": "failed", "error": "approval_denied"},
                )
        except Exception:
            self._logger.exception(
                "approval_resume_finalize_failed",
                extra={
                    "msg": f"审批恢复收束任务与运行状态失败，run_id={run_id}，decision={decision}",
                    "data": {"run_id": run_id, "decision": decision},
                },
            )
            raise

    async def run_task(self, task_id: str) -> AsyncIterator[RuntimeEvent]:
        """通过任务的首个轮次运行或回放任务。

        参数:
            task_id: 待执行任务的标识符。

        生成:
            表示模型增量与运行状态变化的 RuntimeEvent 值。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            委托到首个 turn 执行或回放，用于低层运行时兼容测试。
        """

        turn = self._task_store.get_turn_for_task(task_id)
        async for event in self.run_turn(turn.turn_id):
            yield event

    async def run_turn(self, turn_id: str, turn: Optional[TurnRecord] = None) -> AsyncIterator[RuntimeEvent]:
        """运行一个轮次并流式产出运行时事件。

        参数:
            turn_id: 待执行或回放的轮次标识符。
            turn: 可选，调用方已取出的轮次记录。传入可避免重复查询存储；
                为 ``None`` 时本方法会自行按 ``turn_id`` 取库。

        生成:
            表示模型增量、工具活动与终态变化的 RuntimeEvent 值。

        异常:
            KeyError: 如果轮次或任务不存在。

        副作用:
            根据 turn 状态启动运行、接入既有事件回放，或回放终态事件。
        """

        if turn is None:
            turn = self._task_store.get_turn(turn_id)
        task_id = turn.task_id
        task = self._task_store.get_task(task_id)
        if task.status == "cancelled":
            if turn.status != "cancelled":
                self._task_store.update_turn_status(turn.turn_id, "cancelled")
            existing_events = self._task_store.list_events_for_turn(turn.turn_id) or self._task_store.list_events(task.task_id)
            for event in existing_events:
                yield event
            return
        if turn.status == "running":
            async for event in self._stream_running_turn_events(turn.turn_id):
                yield event
            return
        if turn.status != "pending":
            existing_events = self._task_store.list_events_for_turn(turn.turn_id)
            if existing_events:
                for event in existing_events:
                    yield event
                return
            yield self._record(
                EventType.RUN_FAILED,
                task.task_id,
                {"status": turn.status, "error": "turn is not pending", "_turn_id": turn.turn_id},
            )
            return

        agent_profile = self._resolve_task_agent_profile(task)
        if agent_profile is None:
            self._task_store.update_status(task.task_id, "failed")
            self._task_store.update_turn_status(turn.turn_id, "failed")
            self._mark_run_for_turn(
                turn.turn_id,
                "failed",
                interruption_reason="agent_profile_unavailable",
            )
            self._logger.error(
                "agent_profile_unavailable",
                extra=trace_log_extra(
                    self._trace_context_for_task(task.task_id),
                    msg=f"任务绑定的 Agent 档案不可用，无法执行，task_id={task.task_id}",
                    data={
                        "task_id": task.task_id,
                        "task_agent_id": task.agent_id,
                        "runtime_agent_id": self._agent_profile.agent_id,
                    },
                ),
            )
            yield self._record(
                EventType.RUN_FAILED,
                task.task_id,
                {
                    "status": "failed",
                    "error": "agent_profile_unavailable",
                    "task_agent_id": task.agent_id,
                    "runtime_agent_id": self._agent_profile.agent_id,
                    "_turn_id": turn.turn_id,
                },
            )
            return

        if not self._task_store.claim_pending_turn(turn.turn_id):
            async for event in self._stream_running_turn_events(turn.turn_id):
                yield event
            return
        self._task_store.update_status(task.task_id, "running")
        self._mark_run_for_turn(turn.turn_id, "running")
        self._record_langgraph_lifecycle(turn.turn_id, "run_started", "running")
        yield self._record(
            EventType.RUN_STARTED,
            task.task_id,
            {"status": "running", "agent": agent_profile.to_dict(), "_turn_id": turn.turn_id},
        )

        run = self._run_store.get_by_turn(turn.turn_id) if self._run_store is not None else None
        operations = RuntimeOperations(
            settings=self._settings,
            task_store=self._task_store,
            context_builder=self._context_builder,
            model_adapter=self._model_adapter,
            tool_scheduler=self._tool_scheduler,
            logger=self._logger,
            agent_profile=agent_profile,
            record_event=self._record,
            current_turn_id=turn.turn_id,
            tool_runtime=self._tool_runtime,
            tool_run_id=run.run_id if run is not None else "",
        )
        yield operations.create_checkpoint(task.task_id, "run_started")

        try:
            async for event in self._workflow.run(task, operations):
                yield event
            final_task = self._task_store.get_task(task.task_id)
            if final_task.status in {"completed", "failed", "cancelled"}:
                self._task_store.update_turn_status(turn.turn_id, final_task.status)
            self._sync_run_with_turn_status(turn.turn_id, final_task)
            return
        except Exception as exc:
            self._task_store.update_status(task.task_id, "failed")
            self._task_store.update_turn_status(turn.turn_id, "failed")
            self._mark_run_for_turn(turn.turn_id, "failed", interruption_reason=str(exc))
            self._task_store.update_steps_status_for_task(
                task.task_id,
                "running",
                "failed",
                str(exc),
            )
            self._logger.exception(
                "task_failed",
                extra=trace_log_extra(
                    self._trace_context_for_task(task.task_id, run.run_id if run is not None else ""),
                    msg=f"任务执行失败，task_id={task.task_id}",
                    data={"task_id": task.task_id},
                ),
            )
            yield operations.create_checkpoint(task.task_id, "run_failed")
            yield self._record(
                EventType.RUN_FAILED,
                task.task_id,
                {"status": "failed", "error": str(exc), "_turn_id": turn.turn_id},
            )

    async def _stream_running_turn_events(self, turn_id: str) -> AsyncIterator[RuntimeEvent]:
        """接入运行中轮次并持续输出新事件直到轮次结束。

        参数:
            turn_id: 需要接入的运行中轮次标识符。

        生成:
            已落库和后续追加的 RuntimeEvent。

        异常:
            KeyError: 如果轮次不存在。

        副作用:
            轮询事件存储直到轮次状态离开 running。
        """

        seen_event_ids: set[str] = set()
        while True:
            for event in self._task_store.list_events_for_turn(turn_id):
                if event.event_id not in seen_event_ids:
                    seen_event_ids.add(event.event_id)
                    yield event
            latest_turn = self._task_store.get_turn(turn_id)
            if latest_turn.status != "running":
                break
            await asyncio.sleep(0.1)

    def _sync_run_with_turn_status(self, turn_id: str, task: TaskRecord) -> None:
        """根据当前轮次执行后的任务终态同步 Durable Run 状态。

        参数:
            turn_id: 当前运行轮次标识符。
            task: 已执行完一次工作流后的任务记录。

        返回:
            无。

        异常:
            无。非法状态流转会被记录为错误日志，避免破坏事件流。

        副作用:
            在配置 Durable Run State 时更新运行状态并写入日志。
        """

        if task.status in {"completed", "failed", "cancelled"}:
            self._mark_run_for_turn(turn_id, task.status)
            self._record_langgraph_lifecycle(turn_id, f"run_{task.status}", task.status)

    def _mark_run_for_turn(
        self,
        turn_id: str,
        status: str,
        wait_reason: Optional[str] = None,
        active_step_id: Optional[str] = None,
        active_wait_id: Optional[str] = None,
        interruption_reason: Optional[str] = None,
    ) -> None:
        """按轮次标识同步 Durable Run 状态。

        参数:
            turn_id: 关联轮次标识符。
            status: 目标运行状态。
            wait_reason: 等待原因。
            active_step_id: 当前活跃步骤标识符。
            active_wait_id: 当前等待点标识符。
            interruption_reason: 中断、失败或取消原因。

        返回:
            无。

        异常:
            无。缺失 run、非法流转和数据库错误都会写入日志后返回。

        副作用:
            可能更新 durable_runs 表并写入诊断日志。
        """

        if self._run_store is None:
            return
        try:
            run = self._run_store.get_by_turn(turn_id)
            if run is None:
                self._logger.warning(
                    "durable_run_missing",
                    extra={
                        "msg": f"轮次未绑定 Durable Run，跳过状态同步，turn_id={turn_id}",
                        "data": {"turn_id": turn_id, "target_status": status},
                    },
                )
                return
            self._run_store.mark_status(
                run.run_id,
                status,
                wait_reason=wait_reason,
                active_step_id=active_step_id,
                active_wait_id=active_wait_id,
                interruption_reason=interruption_reason,
            )
        except Exception:
            self._logger.exception(
                "durable_run_status_sync_failed",
                extra={
                    "msg": f"同步 Durable Run 状态失败，turn_id={turn_id}，target_status={status}",
                    "data": {"turn_id": turn_id, "target_status": status},
                },
            )

    def _record_langgraph_lifecycle(self, turn_id: str, phase: str, task_status: str) -> None:
        """将运行生命周期阶段写入 LangGraph checkpointer。

        参数:
            turn_id: 关联轮次标识符。
            phase: 当前生命周期阶段。
            task_status: 当前任务状态。

        返回:
            无。

        异常:
            无。LangGraph 不可用或调用失败时只写入日志。

        副作用:
            在配置 LangGraph Runtime 时调用 graph，并写入 LangGraph checkpoint。
        """

        if self._langgraph_runtime is None or self._run_store is None:
            return
        try:
            run = self._run_store.get_by_turn(turn_id)
            if run is None:
                return
            self._langgraph_runtime.invoke(
                run.thread_id,
                input_value={
                    "run_id": run.run_id,
                    "task_id": run.task_id,
                    "turn_id": turn_id,
                    "phase": phase,
                    "task_status": task_status,
                    "payload": {},
                },
            )
        except Exception:
            self._logger.exception(
                "langgraph_lifecycle_record_failed",
                extra={
                    "msg": f"写入 LangGraph 生命周期检查点失败，turn_id={turn_id}，phase={phase}",
                    "data": {"turn_id": turn_id, "phase": phase},
                },
            )

    def _resume_langgraph_for_approval(self, approval_id: str, decision_payload: dict) -> None:
        """将审批决策作为 LangGraph resume 值写回对应 thread。

        参数:
            approval_id: 审批请求标识符。
            decision_payload: 已持久化的审批决策字典。

        返回:
            无。

        异常:
            无。审批请求、运行记录或 LangGraph 调用失败时只写入日志。

        副作用:
            在配置 LangGraph Runtime 时调用 ``Command(resume=...)`` 恢复对应 graph。
        """

        if (
            self._langgraph_runtime is None
            or self._run_store is None
            or self._approval_service is None
        ):
            return
        try:
            approval = self._approval_service.get_request(approval_id)
            run = self._run_store.get(approval.run_id)
            self._langgraph_runtime.invoke(
                run.thread_id,
                resume_value={
                    "approval_id": approval_id,
                    "decision": decision_payload,
                },
            )
        except Exception:
            self._logger.exception(
                "langgraph_approval_resume_failed",
                extra={
                    "msg": f"将审批决策写回 LangGraph resume 失败，approval_id={approval_id}",
                    "data": {"approval_id": approval_id},
                },
            )

    def list_events(self, task_id: str) -> list:
        """返回单个任务的已存储事件。

        参数:
            task_id: 需要返回其事件时间线的任务标识符。

        返回:
            为该任务存储的运行时事件列表。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        return self._task_store.list_events(task_id)

    def list_steps(self, task_id: str) -> list:
        """返回单个任务的已存储步骤。

        参数:
            task_id: 需要返回其步骤时间线的任务标识符。

        返回:
            为该任务存储的运行时步骤列表。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        return self._task_store.list_steps_for_task(task_id)

    def list_checkpoints(self, task_id: str) -> list:
        """返回单个任务的已存储检查点。

        参数:
            task_id: 需要返回其检查点的任务标识符。

        返回:
            为该任务持久化的检查点记录列表。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        checkpoints: list[CheckpointRecord] = self._task_store.list_checkpoints(task_id)
        return checkpoints

    def get_task(self, task_id: str) -> TaskRecord:
        """按标识符返回任务状态。

        参数:
            task_id: 需要获取的任务标识符。

        返回:
            匹配的任务记录。获取任务列表

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        return self._task_store.get_task(task_id)

    def backend_health(self) -> dict:
        """返回后端模型配置与可用性摘要。

        参数:
            无。

        返回:
            不含 secret 原文的后端健康状态字典，包含 provider、model、base_url、
            API Key 是否已配置等信息。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "status": "ok",
            "model_provider": self._settings.model_provider,
            "model_base_url": self._settings.model_base_url,
            "model_name": self._settings.model_name,
            "model_thinking_mode": self._settings.model_thinking_mode,
            "model_api_key_env": self._settings.model_api_key_env,
            "has_model_api_key": bool(os.environ.get(self._settings.model_api_key_env)),
        }

    def trace_query_service(self) -> TraceQueryService:
        """返回 Trace 查询服务。

        参数:
            无。

        返回:
            配置好的 TraceQueryService。

        异常:
            RuntimeError: 如果当前运行时未配置 Trace Backbone。

        副作用:
            无。
        """

        if self._trace_query_service is None:
            raise RuntimeError("trace query service is not configured")
        return self._trace_query_service

    def log_query_service(self) -> LogQueryService:
        """返回日志查询服务。

        参数:
            无。

        返回:
            配置好的 LogQueryService。

        异常:
            RuntimeError: 如果当前运行时未配置日志查询服务。

        副作用:
            无。
        """

        if self._log_query_service is None:
            raise RuntimeError("log query service is not configured")
        return self._log_query_service

    def logger(self) -> logging.Logger:
        """返回运行时使用的后端 logger。

        参数:
            无。

        返回:
            后端 logger。

        异常:
            无。

        副作用:
            无。
        """

        return self._logger

    def replay_service(self) -> ReplayService:
        """返回 Agent Replay 查询服务。

        参数:
            无。

        返回:
            配置好的 ReplayService。

        异常:
            RuntimeError: 如果当前运行时未配置 Agent Replay。

        副作用:
            无。
        """

        if self._replay_service is None:
            raise RuntimeError("replay service is not configured")
        return self._replay_service

    def _trace_context_for_task(self, task_id: str, run_id: str = ""):
        """返回任务对应的 trace 上下文。

        参数:
            task_id: 任务标识。
            run_id: 可选 Durable Run 标识。

        返回:
            TraceRecorder 提供的稳定上下文；未配置 TraceRecorder 时返回空 trace_id 上下文。

        异常:
            无。

        副作用:
            配置 TraceRecorder 时可能首次生成 trace_id。
        """

        if self._trace_recorder is not None:
            context = self._trace_recorder.context_for_task(task_id, run_id)
            bind_log_context(context)
            return context
        from app.core.trace.context import TraceContext

        return TraceContext(trace_id="", task_id=task_id, run_id=run_id)

    def _resolve_task_agent_profile(self, task: TaskRecord) -> Optional[AgentProfile]:
        """返回被允许执行该任务的 Agent 档案。

        参数:
            task: 被选中进行执行的待执行任务。

        返回:
            当当前运行时 Agent 档案与任务持久化的 ``agent_id`` 匹配时返回该档案，
            否则返回 ``None``。

        异常:
            无。

        副作用:
            无。
        """

        if task.agent_id == self._agent_profile.agent_id:
            return self._agent_profile
        return None

    def _record(self, event_type: EventType, task_id: str, payload: dict) -> RuntimeEvent:
        """创建、存储并记录一个运行时事件。

        参数:
            event_type: 稳定的事件类型枚举成员。
            task_id: 与该事件关联的任务标识符。
            payload: 可序列化为 JSON 的事件载荷。

        返回:
            已创建的运行时事件。

        异常:
            KeyError: 如果任务在存储中不存在。

        副作用:
            将事件追加到存储并写入一条 info 日志记录。
        """

        payload = dict(payload)
        turn_id = payload.pop("_turn_id", None)
        tool_call_id = payload.get("tool_call_id")
        event = RuntimeEvent(
            event_type=event_type,
            task_id=task_id,
            turn_id=turn_id,
            sequence=self._task_store.next_event_sequence(task_id),
            tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
            payload=payload,
        )
        self._task_store.append_event(event)
        run = self._run_store.get_by_turn(turn_id) if self._run_store is not None and turn_id else None
        if self._trace_recorder is not None:
            context = self._trace_context_for_task(task_id, run.run_id if run is not None else "")
            trace_event_name = runtime_trace_event_name(str(event_type), payload)
            if trace_event_name:
                self._trace_recorder.record_event(
                    context,
                    trace_event_name,
                    {"runtime_event_id": event.event_id, **payload},
                    source="runtime",
                    level=_trace_level_for_runtime_event(event_type, payload),
                )
        self._logger.info(
            "runtime_event",
            extra=trace_log_extra(
                self._trace_context_for_task(task_id, run.run_id if run is not None else ""),
                msg=f"运行时事件已记录，event_type={event_type}，task_id={task_id}",
                data={
                    "task_id": task_id,
                    "run_id": run.run_id if run is not None else "",
                    "event_type": str(event_type),
                    "event_id": event.event_id,
                },
            ),
        )
        return event
def _trace_level_for_runtime_event(event_type: EventType, payload: dict) -> str:
    """返回运行时事件写入 trace 时使用的日志级别。

    参数:
        event_type: RuntimeEvent 类型。
        payload: RuntimeEvent 载荷。

    返回:
        error 或 info。

    异常:
        无。

    副作用:
        无。
    """

    if event_type == EventType.RUN_FAILED:
        return "error"
    if event_type == EventType.TOOL_CALL_FINISHED and str(payload.get("status") or "") == "error":
        return "error"
    return "info"
