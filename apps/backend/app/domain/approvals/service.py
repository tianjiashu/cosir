"""工具审批业务服务。"""

import logging
from typing import Any, Dict, Optional

from app.domain.approvals.records import ApprovalDecisionRecord, ApprovalRequestRecord
from app.storage.crud.approval import ApprovalStore
from app.core.runs.langgraph_runtime import LangGraphRuntime
from app.core.runs.resume import ResumeDispatcher
from app.core.runs.state_machine import RunStateMachine
from app.storage.crud.durable import DurableRunStore
from app.core.trace.recorder import TraceRecorder
from app.config.logging import merge_log_context, reset_log_context


class ApprovalService:
    """连接审批请求、运行等待状态和恢复命令。"""

    def __init__(
        self,
        approval_store: ApprovalStore,
        run_store: DurableRunStore,
        resume_dispatcher: ResumeDispatcher,
        logger: logging.Logger,
        langgraph_runtime: Optional[LangGraphRuntime] = None,
        trace_recorder: Optional[TraceRecorder] = None,
    ) -> None:
        """初始化审批服务。

        参数:
            approval_store: 审批请求仓储。
            run_store: Durable Run State 仓储。
            resume_dispatcher: 恢复命令分发器。
            logger: 日志器。
            langgraph_runtime: 可选的 LangGraph 生命周期运行时。
            trace_recorder: 可选 Trace Backbone 写入器。

        返回:
            无。

        异常:
            无。

        副作用:
            保存依赖项。
        """

        self._approval_store = approval_store
        self._run_store = run_store
        self._resume_dispatcher = resume_dispatcher
        self._logger = logger
        self._langgraph_runtime = langgraph_runtime
        self._trace_recorder = trace_recorder
        self._run_state_machine = RunStateMachine()

    def delete_run_data(self, run_ids: list[str]) -> None:
        """删除运行集合下的审批请求与决策。

        参数:
            run_ids: 需要清理的 Durable Run 标识符列表。

        返回:
            无。

        异常:
            无。底层数据库异常会向调用方抛出。

        副作用:
            删除审批请求和审批决策记录。
        """

        if not run_ids:
            return
        try:
            self._approval_store.delete_by_run_ids(run_ids)
        except Exception:
            self._logger.exception(
                "approval_data_delete_failed",
                extra={
                    "msg": f"删除运行集合下的审批请求与决策失败",
                    "data": {"run_count": len(run_ids), "operation": "delete_run_data"},
                },
            )
            raise
        self._logger.info(
            "approval_data_deleted",
            extra={
                "msg": f"运行集合下的审批请求与决策已删除",
                "data": {"run_count": len(run_ids)},
            },
        )

    def request_approval(
        self,
        run_id: str,
        tool_name: str,
        permission: str,
        risk_level: str,
        payload: Dict[str, Any],
        step_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> ApprovalRequestRecord:
        """创建审批请求并将运行标记为等待审批。

        参数:
            run_id: 运行标识符。
            tool_name: 工具名称。
            permission: 权限级别。
            risk_level: 风险等级。
            payload: 审批展示载荷。
            step_id: 可选步骤标识符。
            tool_call_id: 可选工具调用标识符。

        返回:
            创建的审批请求。

        异常:
            KeyError: 如果运行不存在。
            InvalidRunTransition: 如果当前运行状态不能进入 waiting。

        副作用:
            写入审批请求、更新运行等待状态并记录日志。
        """

        run = self._run_store.get(run_id)
        self._run_state_machine.ensure_transition(run.status, "waiting")
        approval = self._approval_store.create_request(
            run_id=run_id,
            tool_name=tool_name,
            permission=permission,
            risk_level=risk_level,
            payload=payload,
            status="pending",
            step_id=step_id,
            tool_call_id=tool_call_id,
        )
        self._run_store.mark_status(
            run_id,
            "waiting",
            wait_reason="approval",
            active_step_id=step_id,
            active_wait_id=approval.approval_id,
        )
        token = merge_log_context(run_id=run_id)
        try:
            self._logger.info(
                "approval_requested",
                extra={
                    "msg": f"审批请求已创建，run_id={run_id}，tool={tool_name}",
                    "data": {
                        "tool_name": tool_name,
                        "permission": permission,
                        "risk_level": risk_level,
                        "approval_id": approval.approval_id,
                        "tool_call_id": approval.tool_call_id,
                    },
                },
            )
            self._record_approval_event(
                approval,
                "approval_requested",
                {
                    "approval_id": approval.approval_id,
                    "tool_call_id": approval.tool_call_id,
                    "tool_name": approval.tool_name,
                    "permission": approval.permission,
                    "risk_level": approval.risk_level,
                    "step_id": approval.step_id,
                },
            )
            self._interrupt_langgraph_for_approval(approval)
        finally:
            reset_log_context(token)
        return approval

    def _interrupt_langgraph_for_approval(self, approval: ApprovalRequestRecord) -> None:
        """在 LangGraph lifecycle graph 中创建审批等待点。

        参数:
            approval: 已持久化的审批请求。

        返回:
            无。

        异常:
            无。LangGraph 不可用或调用失败时只写入日志。

        副作用:
            当配置 LangGraph Runtime 时调用 graph，并通过 interrupt 暂停对应 thread。
        """

        if self._langgraph_runtime is None:
            return
        try:
            run = self._run_store.get(approval.run_id)
            self._langgraph_runtime.invoke(
                run.thread_id,
                input_value={
                    "run_id": run.run_id,
                    "task_id": run.task_id,
                    "phase": "waiting_approval",
                    "task_status": "waiting",
                    "payload": approval.to_dict(),
                },
            )
        except Exception as exc:
            self._logger.exception(
                "langgraph_approval_interrupt_failed",
                extra={
                    "msg": f"为审批创建 LangGraph 等待点失败，run_id={approval.run_id}",
                    "data": {
                        "run_id": approval.run_id,
                        "approval_id": approval.approval_id,
                    },
                },
            )

    def list_pending(self, run_id: Optional[str] = None) -> list[ApprovalRequestRecord]:
        """列出待处理审批请求。

        参数:
            run_id: 可选运行标识符；提供时只返回该运行的审批请求。

        返回:
            待处理审批请求列表。

        异常:
            Exception: 如果底层存储查询失败。

        副作用:
            无。
        """

        return self._approval_store.list_by_status("pending", run_id)

    def get_request(self, approval_id: str) -> ApprovalRequestRecord:
        """按审批标识返回审批请求。

        参数:
            approval_id: 审批请求标识符。

        返回:
            匹配的审批请求记录。

        异常:
            KeyError: 如果审批请求不存在。

        副作用:
            无。
        """

        return self._approval_store.get_request(approval_id)

    def get_decision(self, approval_id: str) -> Optional[ApprovalDecisionRecord]:
        """返回指定审批请求已经持久化的决策。

        参数:
            approval_id: 审批请求标识。

        返回:
            存在时返回审批决策，否则返回 None。

        异常:
            Exception: 如果底层存储查询失败。

        副作用:
            无。
        """

        return self._approval_store.get_decision_by_approval(approval_id)

    def decide(
        self,
        approval_id: str,
        decision: str,
        reason: Optional[str],
        idempotency_key: str,
    ) -> ApprovalDecisionRecord:
        """记录审批决策并分发恢复命令。

        参数:
            approval_id: 审批请求标识符。
            decision: approved 或 denied。
            reason: 决策原因。
            idempotency_key: 幂等键。

        返回:
            审批决策记录。

        异常:
            ValueError: 如果 decision 非法。
            KeyError: 如果审批请求不存在。

        副作用:
            写入审批决策、创建恢复命令并记录日志。
        """

        if decision not in {"approved", "denied"}:
            raise ValueError("decision must be approved or denied")
        approval = self._approval_store.get_request(approval_id)
        action = "approve_tool" if decision == "approved" else "deny_tool"
        run = self._run_store.get(approval.run_id)
        self._run_state_machine.ensure_transition(run.status, "resuming")
        record = self._approval_store.create_decision(
            approval_id=approval_id,
            decision=decision,
            reason=reason,
            idempotency_key=idempotency_key,
        )
        if record.approval_id != approval_id:
            raise ValueError("idempotency key is already used by a different approval")
        if record.decision != decision:
            raise ValueError("approval already decided with a different decision")
        command = self._resume_dispatcher.dispatch(
            run_id=approval.run_id,
            action=action,
            payload={"approval_id": approval_id, "decision": decision, "reason": reason},
            idempotency_key=f"resume:{record.idempotency_key}",
        )
        self._approval_store.update_request_status(approval_id, decision, record.decided_at)
        token = merge_log_context(run_id=approval.run_id)
        try:
            self._logger.info(
                "approval_decided",
                extra={
                    "msg": f"审批决策已记录，decision={decision}，approval_id={approval_id}",
                    "data": {
                        "decision": decision,
                        "approval_id": approval_id,
                        "tool_call_id": approval.tool_call_id,
                        "resume_command_id": command.command_id,
                    },
                },
            )
            self._record_approval_event(
                approval,
                "approval_decided",
                {
                    "approval_id": approval_id,
                    "tool_call_id": approval.tool_call_id,
                    "decision": decision,
                    "reason": reason,
                    "resume_command_id": command.command_id,
                    "idempotency_key": idempotency_key,
                },
            )
        finally:
            reset_log_context(token)
        return record

    def _record_approval_event(
        self,
        approval: ApprovalRequestRecord,
        event_name: str,
        payload: dict[str, Any],
    ) -> None:
        """向 Trace Backbone 写入审批领域事件。

        参数:
            approval: 审批请求记录。
            event_name: canonical trace event 名称。
            payload: 事件载荷。

        返回:
            无。

        异常:
            KeyError: 如果审批关联的 run 不存在。

        副作用:
            配置 TraceRecorder 时追加 trace event；trace 写入失败由 TraceRecorder 自行记录。
        """

        if self._trace_recorder is None:
            return
        run = self._run_store.get(approval.run_id)
        context = self._trace_recorder.context_for_task(run.task_id, approval.run_id)
        self._trace_recorder.record_event(context, event_name, payload, source="approval")
