"""协调任务生命周期与工作流执行。"""

import logging
import os
from typing import AsyncIterator, Optional

from app.agents.profile import AgentProfile, default_developer_agent
from app.approvals.service import ApprovalService
from app.config.settings import BackendSettings
from app.context.builder import TextContextBuilder
from app.events.types import EventType, RuntimeEvent
from app.runs.langgraph_runtime import LangGraphRuntime
from app.runs.recovery import RecoveryManager
from app.runs.store import DurableRunStore
from app.models.base import StreamingModelAdapter
from app.runtime.operations import RuntimeOperations
from app.storage.records import CheckpointRecord, TaskRecord
from app.tools.scheduler import ToolScheduler
from app.tools.runtime import ToolExecutionContext, ToolRuntime
from app.tools.types import ToolCall
from app.workflows.react_like import ReactLikeWorkflow
from app.workflows.types import AgentWorkflow


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
        langgraph_runtime: Optional[LangGraphRuntime] = None,
        tool_runtime: Optional[ToolRuntime] = None,
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
            langgraph_runtime: 可选的 LangGraph 生命周期持久化运行时。
            tool_runtime: 可选 Tool v2 工具执行入口。

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
        self._langgraph_runtime = langgraph_runtime
        self._tool_runtime = tool_runtime

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
        except Exception as exc:
            self._logger.exception("runtime_close_failed error=%s", exc)

    def create_task(self, input_text: str, session_id: Optional[str] = None) -> TaskRecord:
        """创建一个待执行的任务，留待后续执行。

        参数:
            input_text: 纯文本的用户任务。
            session_id: 可选的会话标识符。

        返回:
            已创建的任务记录。

        异常:
            ValueError: 如果 ``input_text`` 为空。

        副作用:
            在配置好的任务存储中持久化任务状态，并写入一条 info 日志。
        """

        task = self._task_store.create_task(
            input_text=input_text,
            session_id=session_id,
            agent_id=self._agent_profile.agent_id,
        )
        self._logger.info(
            "task_created task_id=%s session_id=%s agent_id=%s",
            task.task_id,
            session_id,
            self._agent_profile.agent_id,
        )
        if self._run_store is not None:
            run = self._run_store.create_for_task(task.task_id)
            self._logger.info(
                "durable_run_bound task_id=%s run_id=%s thread_id=%s",
                task.task_id,
                run.run_id,
                run.thread_id,
            )
        return task

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

        task = self._task_store.update_status(task_id, "cancelled")
        self._mark_run_for_task(task_id, "cancelled", interruption_reason="task_cancelled")
        self._record(EventType.RUN_CANCELLED, task_id, {"status": "cancelled"})
        self._logger.info("task_cancelled task_id=%s", task_id)
        return task

    def list_recoverable_runs(self) -> list:
        """列出可恢复或需要人工处理的运行记录。

        参数:
            无。

        返回:
            可序列化的运行记录列表。

        异常:
            RuntimeError: 如果运行时未配置 Durable Run State。

        副作用:
            可能执行恢复对账并写入日志。
        """

        if self._run_store is None:
            raise RuntimeError("durable run recovery is not configured")
        return [run.to_dict() for run in self._run_store.list_recoverable()]

    def resume_run(self, run_id: str) -> dict:
        """显式恢复指定运行并消费其待处理恢复命令。

        参数:
            run_id: 需要恢复的 Durable Run 标识符。

        返回:
            恢复动作完成后的运行记录字典。

        异常:
            RuntimeError: 如果运行时未配置 Durable Run State。
            KeyError: 如果运行记录不存在。

        副作用:
            重新排队该 run 的中断命令，消费 pending 恢复命令，并写入状态与事件。
        """

        if self._run_store is None:
            raise RuntimeError("durable run recovery is not configured")
        self._run_store.get(run_id)
        self._run_store.requeue_processing_resume_commands(run_id)
        self._consume_pending_resume_commands(run_id)
        return self._run_store.get(run_id).to_dict()

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
            run = self._run_store.get_by_task(task_id)
            if run is None:
                return []
            approvals = self._approval_service.list_pending(run.run_id)
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
        record = self._approval_service.decide(
            approval_id=approval_id,
            decision=decision,
            reason=reason,
            idempotency_key=idempotency_key,
        )
        approval = self._approval_service.get_request(approval_id)
        self._consume_pending_resume_commands(approval.run_id)
        return record.to_dict()

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
        for command in self._run_store.claim_pending_resume_commands(
            run_id,
            actions=("approve_tool", "deny_tool"),
        ):
            approval_id = command.payload.get("approval_id")
            if command.action not in {"approve_tool", "deny_tool"} or not isinstance(approval_id, str):
                self._logger.error(
                    "resume_command_unsupported run_id=%s command_id=%s action=%s",
                    command.run_id,
                    command.command_id,
                    command.action,
                )
                self._run_store.release_resume_command(command.command_id)
                continue
            try:
                approval = self._approval_service.get_request(approval_id)
                self._resume_approved_tool_call(approval, command.payload.get("decision", ""))
                decision = self._approval_service.get_decision(approval_id)
                if decision is None:
                    raise RuntimeError("approval decision is missing for resume command")
                self._resume_langgraph_for_approval(approval_id, decision.to_dict())
                self._finalize_approval_resume(command.run_id, decision.decision)
                self._run_store.mark_resume_command_applied(command.command_id)
            except Exception:
                self._logger.exception(
                    "resume_command_consume_failed run_id=%s command_id=%s action=%s",
                    command.run_id,
                    command.command_id,
                    command.action,
                )
                self._run_store.release_resume_command(command.command_id)

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
            self._logger.error("tool_resume_payload_invalid run_id=%s approval_id=%s", approval.run_id, approval.approval_id)
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
                {"tool_name": observation.tool_name, "status": observation.status, "error": observation.error},
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
        except Exception as exc:
            self._logger.exception(
                "approval_resume_finalize_failed run_id=%s decision=%s error=%s",
                run_id,
                decision,
                exc,
            )
            raise

    async def run_task(self, task_id: str) -> AsyncIterator[RuntimeEvent]:
        """运行一个任务并流式产出运行时事件。

        参数:
            task_id: 待执行任务的标识符。

        生成:
            表示模型增量与运行状态变化的 RuntimeEvent 值。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            更新任务状态、持久化事件并写入运行时日志。
        """

        task = self._task_store.get_task(task_id)
        if task.status != "pending":
            existing_events = self._task_store.list_events(task.task_id)
            if existing_events:
                for event in existing_events:
                    yield event
                return
            yield self._record(
                EventType.RUN_FAILED,
                task.task_id,
                {"status": task.status, "error": "task is not pending"},
            )
            return

        agent_profile = self._resolve_task_agent_profile(task)
        if agent_profile is None:
            self._task_store.update_status(task.task_id, "failed")
            self._mark_run_for_task(
                task.task_id,
                "failed",
                interruption_reason="agent_profile_unavailable",
            )
            self._logger.error(
                "agent_profile_unavailable task_id=%s task_agent_id=%s runtime_agent_id=%s",
                task.task_id,
                task.agent_id,
                self._agent_profile.agent_id,
            )
            yield self._record(
                EventType.RUN_FAILED,
                task.task_id,
                {
                    "status": "failed",
                    "error": "agent_profile_unavailable",
                    "task_agent_id": task.agent_id,
                    "runtime_agent_id": self._agent_profile.agent_id,
                },
            )
            return

        self._task_store.update_status(task.task_id, "running")
        self._mark_run_for_task(task.task_id, "running")
        self._record_langgraph_lifecycle(task.task_id, "run_started", "running")
        yield self._record(
            EventType.RUN_STARTED,
            task.task_id,
            {"status": "running", "agent": agent_profile.to_dict()},
        )

        run = self._run_store.get_by_task(task.task_id) if self._run_store is not None else None
        operations = RuntimeOperations(
            settings=self._settings,
            task_store=self._task_store,
            context_builder=self._context_builder,
            model_adapter=self._model_adapter,
            tool_scheduler=self._tool_scheduler,
            logger=self._logger,
            agent_profile=agent_profile,
            record_event=self._record,
            tool_runtime=self._tool_runtime,
            tool_run_id=run.run_id if run is not None else "",
        )
        yield operations.create_checkpoint(task.task_id, "run_started")

        try:
            async for event in self._workflow.run(task, operations):
                yield event
            final_task = self._task_store.get_task(task.task_id)
            self._sync_run_with_task_status(final_task)
            return
        except Exception as exc:
            self._task_store.update_status(task.task_id, "failed")
            self._mark_run_for_task(task.task_id, "failed", interruption_reason=str(exc))
            self._task_store.close_running_steps_for_task(
                task.task_id,
                "failed",
                str(exc),
            )
            self._logger.exception("task_failed task_id=%s", task.task_id)
            yield operations.create_checkpoint(task.task_id, "run_failed")
            yield self._record(
                EventType.RUN_FAILED,
                task.task_id,
                {"status": "failed", "error": str(exc)},
            )

    def _sync_run_with_task_status(self, task: TaskRecord) -> None:
        """根据任务终态同步 Durable Run 状态。

        参数:
            task: 已执行完一次工作流后的任务记录。

        返回:
            无。

        异常:
            无。非法状态流转会被记录为错误日志，避免破坏事件流。

        副作用:
            在配置 Durable Run State 时更新运行状态并写入日志。
        """

        if task.status in {"completed", "failed", "cancelled"}:
            self._mark_run_for_task(task.task_id, task.status)
            self._record_langgraph_lifecycle(task.task_id, f"run_{task.status}", task.status)

    def _mark_run_for_task(
        self,
        task_id: str,
        status: str,
        wait_reason: Optional[str] = None,
        active_step_id: Optional[str] = None,
        active_wait_id: Optional[str] = None,
        interruption_reason: Optional[str] = None,
    ) -> None:
        """按任务标识同步 Durable Run 状态。

        参数:
            task_id: 关联任务标识符。
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
            run = self._run_store.get_by_task(task_id)
            if run is None:
                self._logger.warning(
                    "durable_run_missing task_id=%s target_status=%s",
                    task_id,
                    status,
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
        except Exception as exc:
            self._logger.exception(
                "durable_run_status_sync_failed task_id=%s target_status=%s error=%s",
                task_id,
                status,
                exc,
            )

    def _record_langgraph_lifecycle(self, task_id: str, phase: str, task_status: str) -> None:
        """将运行生命周期阶段写入 LangGraph checkpointer。

        参数:
            task_id: 关联任务标识符。
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
            run = self._run_store.get_by_task(task_id)
            if run is None:
                return
            self._langgraph_runtime.invoke(
                run.thread_id,
                input_value={
                    "run_id": run.run_id,
                    "task_id": task_id,
                    "phase": phase,
                    "task_status": task_status,
                    "payload": {},
                },
            )
        except Exception as exc:
            self._logger.exception(
                "langgraph_lifecycle_record_failed task_id=%s phase=%s error=%s",
                task_id,
                phase,
                exc,
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
        except Exception as exc:
            self._logger.exception(
                "langgraph_approval_resume_failed approval_id=%s error=%s",
                approval_id,
                exc,
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
            匹配的任务记录。

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

        event = RuntimeEvent(event_type=event_type, task_id=task_id, payload=payload)
        self._task_store.append_event(event)
        self._logger.info("runtime_event task_id=%s type=%s", task_id, event_type)
        return event
