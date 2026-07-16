"""Durable Run State 启动恢复对账。"""

import logging
from typing import List, Optional

from app.core.runs.records import RunRecord
from app.core.runs.store import DurableRunStore
from app.core.trace.recorder import TraceRecorder


class RecoveryManager:
    """对账可恢复或不确定状态的运行记录。"""

    def __init__(
        self,
        run_store: DurableRunStore,
        logger: logging.Logger,
        trace_recorder: Optional[TraceRecorder] = None,
    ) -> None:
        """初始化恢复管理器。

        参数:
            run_store: Durable Run State 仓储。
            logger: 用于记录恢复对账结果的日志器。
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

    def reconcile(self) -> List[RunRecord]:
        """对账并返回可恢复运行列表。

        参数:
            无。

        返回:
            等待、恢复中、中断或需要复核的运行记录列表。

        异常:
            sqlite3.Error: 如果查询运行状态失败。

        副作用:
            写入恢复对账日志。
        """

        requeued = self._run_store.requeue_processing_resume_commands()
        runs = self._run_store.list_recoverable()
        self._logger.info(
            "run_recovery_commands_requeued",
            extra={"run_id": "*", "count": requeued},
        )
        self._logger.info(
            "run_recovery_reconciled",
            extra={
                "run_id": "*",
                "count": len(runs),
                "recoverable_run_ids": [run.run_id for run in runs],
            },
        )
        for run in runs:
            self._record_recovery_event(
                run,
                {
                    "status": run.status,
                    "requeued_processing_count": requeued,
                    "recoverable_run_count": len(runs),
                },
            )
        return runs

    def requeue_processing_resume_commands(self, run_id: str) -> int:
        """重排指定 run 中遗留的 processing 恢复命令并记录恢复对账事件。

        参数:
            run_id: 需要恢复的 Durable Run 标识。

        返回:
            被重排的恢复命令数量。

        异常:
            KeyError: 如果 run_id 不存在。
            sqlite3.Error: 如果状态更新失败。

        副作用:
            更新恢复命令状态、写入日志，并在配置 TraceRecorder 时追加 recovery_reconciled 事件。
        """

        requeued = self._run_store.requeue_processing_resume_commands(run_id)
        run = self._run_store.get(run_id)
        self._logger.info(
            "run_recovery_commands_requeued",
            extra={"run_id": run_id, "count": requeued},
        )
        self._record_recovery_event(
            run,
            {
                "status": run.status,
                "requeued_processing_count": requeued,
                "recoverable_run_count": 1,
            },
        )
        return requeued

    def _record_recovery_event(self, run: RunRecord, payload: dict) -> None:
        """向 Trace Backbone 写入启动恢复对账事件。

        参数:
            run: 可恢复运行记录。
            payload: 恢复对账载荷。

        返回:
            无。

        异常:
            无。Trace 写入失败由 TraceRecorder 自行记录。

        副作用:
            配置 TraceRecorder 时追加 recovery_reconciled 事件。
        """

        if self._trace_recorder is None:
            return
        context = self._trace_recorder.context_for_task(run.task_id, run.run_id)
        self._trace_recorder.record_event(
            context,
            "recovery_reconciled",
            payload,
            source="recovery",
        )
