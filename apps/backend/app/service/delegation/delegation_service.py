"""委派生命周期领域服务。"""

from app.config.logging.logger import log
from app.models.delegation_record import DelegationRecord
from app.models.result.delegation_acquire_result import (
    REASON_CONCURRENCY_EXCEEDED,
    DelegationAcquireResult,
)
from app.storage.crud.delegation_crud import ACTIVE_DELEGATION_STATUSES, DelegationCrud


class DelegationService:
    """持久化委派生命周期状态。"""

    def __init__(self, delegation_crud: DelegationCrud, **_unused_dependencies: object) -> None:
        """初始化委派生命周期服务。

        参数:
            delegation_crud: delegation 单表持久化协作者。
            **_unused_dependencies: 保留既有装配调用形状的无业务参数。

        副作用:
            保存 delegation CRUD 协作者引用。
        """

        self._delegation_crud = delegation_crud

    def try_create_pending(
        self,
        *,
        task_id: int,
        parent_run_id: int,
        parent_agent_id: str,
        child_agent_id: str,
        prompt: str,
        effective_tools: tuple[str, ...],
        max_concurrency: int,
        **_unused_options: object,
    ) -> DelegationAcquireResult:
        """按并发额度原子地尝试创建 pending delegation。"""

        record = DelegationRecord(
            id=None,
            task_id=task_id,
            parent_run_id=parent_run_id,
            child_run_id=None,
            child_task_id=None,
            parent_agent_id=parent_agent_id,
            child_agent_id=child_agent_id,
            status="pending",
            prompt=prompt,
            summary="",
            error="",
            effective_tools=effective_tools,
        )
        created_id = self._delegation_crud.create_pending_if_slot_available(record, max_concurrency)
        if created_id is None:
            return DelegationAcquireResult(
                acquired=False,
                delegation_id=-1,
                reason=REASON_CONCURRENCY_EXCEEDED,
            )
        return DelegationAcquireResult(acquired=True, delegation_id=created_id, reason="")

    def mark_child_started(
        self,
        delegation_id: int,
        child_run_id: int,
        child_task_id: int | None = None,
        **_unused_options: object,
    ) -> None:
        """把 delegation 标记为 running。"""

        self._delegation_crud.update_status(
            delegation_id,
            "running",
            child_run_id=child_run_id,
            child_task_id=child_task_id,
        )

    def mark_completed(
        self,
        delegation_id: int,
        summary: str,
        child_task_id: int | None = None,
        **_unused_options: object,
    ) -> None:
        """把 delegation 标记为 completed。"""

        self._delegation_crud.update_status(
            delegation_id,
            "completed",
            summary=summary,
            child_task_id=child_task_id,
        )

    def mark_failed(
        self,
        delegation_id: int,
        error: str,
        child_task_id: str | None = None,
        **_unused_options: object,
    ) -> None:
        """把 delegation 标记为 failed。"""

        self._delegation_crud.update_status(
            delegation_id, "failed", error=error, child_task_id=child_task_id
        )

    def mark_cancelled(
        self,
        delegation_id: int,
        error: str,
        child_task_id: str | None = None,
        **_unused_options: object,
    ) -> None:
        """把 delegation 标记为 cancelled。"""

        self._delegation_crud.update_status(
            delegation_id, "cancelled", error=error, child_task_id=child_task_id
        )

    def mark_interrupted_delegations_failed(self, reason: str) -> int:
        """原子落定进程中断遗留的 active delegation。"""

        failed_ids = self._delegation_crud.fail_active_delegations(error=reason)
        log.info(
            "delegation_recovery_audit_completed",
            extra={
                "msg": "委派恢复审计完成，已将中断的委派标记为失败",
                "data": {
                    "reason": reason,
                    "recovered_count": len(failed_ids),
                    "delegation_ids": failed_ids,
                },
            },
        )
        return len(failed_ids)

    def list_by_parent_turn(self, parent_run_id: int) -> list[DelegationRecord]:
        """列出某个 parent turn 下的全部 delegation 记录。"""

        return self._delegation_crud.list_by_parent_turn(parent_run_id)

    def list_active_by_parent_turn(self, parent_run_id: int) -> list[DelegationRecord]:
        """列出某个 parent turn 下仍处于活动状态的 delegation。"""

        return [
            record
            for record in self._delegation_crud.list_by_parent_turn(parent_run_id)
            if record.status in ACTIVE_DELEGATION_STATUSES
        ]
