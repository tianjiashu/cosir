"""Durable Run State 恢复命令分发。"""

import logging
from typing import Any, Dict

from app.runs.records import ResumeCommandRecord
from app.runs.store import DurableRunStore


class ResumeDispatcher:
    """接收恢复命令并推进 run 状态。"""

    def __init__(self, run_store: DurableRunStore, logger: logging.Logger) -> None:
        """初始化恢复命令分发器。

        参数:
            run_store: Durable Run State 仓储。
            logger: 用于记录恢复命令的日志器。

        返回:
            无。

        异常:
            无。

        副作用:
            保存依赖项。
        """

        self._run_store = run_store
        self._logger = logger

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
        self._logger.info("resume_dispatched run_id=%s action=%s", run_id, action)
        return command
