"""Finite startup recovery for orphaned Child Agent Runs."""

from __future__ import annotations

from collections.abc import Iterable

from app.config.logging.logger import log
from app.models import ConversationRunRecord, ConversationRunStatus


class ChildAgentRecoveryHook:
    """Close unfinished children of the latest parent Run without live callbacks."""

    def __init__(self, *, task_service: object, run_state_service: object) -> None:
        self._tasks = task_service
        self._run_state = run_state_service

    def recover(self, latest_parent_runs: Iterable[ConversationRunRecord]) -> int:
        """Conditionally cancel active child Runs and return the number changed."""

        recovered = 0
        for parent_run in latest_parent_runs:
            parent_task = self._tasks.get_task(parent_run.task_id)
            for child_task in self._tasks.list_child_tasks(parent_task.id, parent_run.id):
                for child_run in self._tasks.list_runs_for_task(child_task.id):
                    if child_run.status not in {
                        ConversationRunStatus.PENDING.value,
                        ConversationRunStatus.RUNNING.value,
                    }:
                        continue
                    settled = self._run_state.cancel_run_for_startup_recovery(
                        child_run.id, end_reason="runtime_restarted"
                    )
                    if settled is not None:
                        recovered += 1
                        log.info(
                            "orphaned_child_agents_recovered",
                            extra={
                                "msg": "启动恢复已收敛遗留 Child Agent Run",
                                "data": {
                                    "parent_run_id": parent_run.id,
                                    "child_task_id": child_task.id,
                                    "child_run_id": child_run.id,
                                },
                            },
                        )
        return recovered
