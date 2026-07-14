"""Durable Run State 启动恢复对账。"""

import logging
from typing import List

from app.core.runs.records import RunRecord
from app.core.runs.store import DurableRunStore


class RecoveryManager:
    """对账可恢复或不确定状态的运行记录。"""

    def __init__(self, run_store: DurableRunStore, logger: logging.Logger) -> None:
        """初始化恢复管理器。

        参数:
            run_store: Durable Run State 仓储。
            logger: 用于记录恢复对账结果的日志器。

        返回:
            无。

        异常:
            无。

        副作用:
            保存依赖项。
        """

        self._run_store = run_store
        self._logger = logger

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
        self._logger.info("run_recovery_commands_requeued run_id=* count=%s", requeued)
        self._logger.info(
            "run_recovery_reconciled run_id=* count=%s recoverable_run_ids=%s",
            len(runs),
            [run.run_id for run in runs],
        )
        return runs
