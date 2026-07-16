"""Durable Run State 恢复命令分发。"""

import logging
from typing import Any, Dict, Optional

from app.core.runs.records import ResumeCommandRecord
from app.core.runs.store import DurableRunStore
from app.core.trace.recorder import TraceRecorder
from app.config.logging import merge_log_context, reset_log_context


class ResumeDispatcher:
    """接收恢复命令并推进 run 状态。"""

    def __init__(
        self,
        run_store: DurableRunStore,
        logger: logging.Logger,
        trace_recorder: Optional[TraceRecorder] = None,
    ) -> None:
        """初始化恢复命令分发器。

        参数:
            run_store: Durable Run State 仓储。
            logger: 用于记录恢复命令的日志器。
            trace_recorder: 可选 Trace Backbone 写入器。

        返回:
            无。

        异常:
            无。

        副作用:
            保存依赖项。
        """

        self._run_store = run_store
        self._logger = logger
        self._trace_recorder = trace_recorder

    def dispatch(
        self,
        run_id: str,
        action: str,
        payload: Dict[str, Any],
        idempotency_key: str,
    ) -> ResumeCommandRecord:
        """创建恢复命令并将 run 标记为 resuming。

        参数:
            run_id: 被恢复的运行标识符。
            action: 恢复动作。
            payload: 恢复动作载荷。
            idempotency_key: 幂等键。

        返回:
            创建或复用的恢复命令。

        异常:
            KeyError: 如果运行记录不存在。
            InvalidRunTransition: 如果当前状态不能进入 resuming。

        副作用:
            写入恢复命令、更新运行状态并记录日志。
        """

        command = self._run_store.create_resume_command(
            run_id=run_id,
            action=action,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        self._run_store.mark_status(run_id, "resuming")
        token = merge_log_context(run_id=run_id)
        try:
            self._logger.info("resume_dispatched", extra={"action": action, "resume_command_id": command.command_id})
        finally:
            reset_log_context(token)
        return command

    def record_started(self, command: ResumeCommandRecord) -> None:
        """记录恢复命令开始消费。

        参数:
            command: 已被领取的恢复命令。

        返回:
            无。

        异常:
            无。Trace 写入失败只记录日志。

        副作用:
            配置 TraceRecorder 时追加 resume_started 事件。
        """

        self._record_resume_event(
            command,
            "resume_started",
            {"resume_command_id": command.command_id, "action": command.action},
        )

    def record_completed(self, command: ResumeCommandRecord, decision: str = "") -> None:
        """记录恢复命令消费完成。

        参数:
            command: 已完成的恢复命令。
            decision: 可选审批决策结果。

        返回:
            无。

        异常:
            无。Trace 写入失败只记录日志。

        副作用:
            配置 TraceRecorder 时追加 resume_completed 事件。
        """

        payload = {"resume_command_id": command.command_id, "action": command.action}
        if decision:
            payload["decision"] = decision
        self._record_resume_event(command, "resume_completed", payload)

    def record_failed(self, command: ResumeCommandRecord, error: str) -> None:
        """记录恢复命令消费失败。

        参数:
            command: 消费失败的恢复命令。
            error: 失败原因。

        返回:
            无。

        异常:
            无。Trace 写入失败只记录日志。

        副作用:
            配置 TraceRecorder 时追加 resume_failed 事件。
        """

        self._record_resume_event(
            command,
            "resume_failed",
            {"resume_command_id": command.command_id, "action": command.action, "error": error},
            level="error",
        )

    def _record_resume_event(
        self,
        command: ResumeCommandRecord,
        event_name: str,
        payload: dict[str, Any],
        level: str = "info",
    ) -> None:
        """向 Trace Backbone 写入恢复命令事件。

        参数:
            command: 恢复命令记录。
            event_name: canonical trace event 名称。
            payload: 事件载荷。
            level: trace event 级别。

        返回:
            无。

        异常:
            KeyError: 如果恢复命令关联的 run 不存在。

        副作用:
            配置 TraceRecorder 时追加 trace event；trace 写入失败由 TraceRecorder 自行记录。
        """

        if self._trace_recorder is None:
            return
        run = self._run_store.get(command.run_id)
        context = self._trace_recorder.context_for_task(run.task_id, command.run_id)
        self._trace_recorder.record_event(context, event_name, payload, source="resume", level=level)
