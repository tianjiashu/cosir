"""Delegation lifecycle service."""

import asyncio
from uuid import uuid4

from app.config.logging.logger import log
from app.models.delegation_record import DelegationRecord
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.payload.delegation_cancelled_payload import DelegationCancelledPayload
from app.models.payload.delegation_child_started_payload import DelegationChildStartedPayload
from app.models.payload.delegation_failed_payload import DelegationFailedPayload
from app.models.payload.delegation_finished_payload import DelegationFinishedPayload
from app.models.payload.delegation_started_payload import DelegationStartedPayload
from app.service.agent_runtime_event.runtime_event_service import RuntimeEventService
from app.models.result.delegation_acquire_result import (
    REASON_CONCURRENCY_EXCEEDED,
    DelegationAcquireResult,
)
from app.storage.crud.delegation_crud import ACTIVE_DELEGATION_STATUSES, DelegationCrud
from app.utils.datetime_utils import utc_now


class DelegationService:
    """Persist delegation lifecycle state and parent-turn runtime events."""

    def __init__(
        self,
        delegation_crud: DelegationCrud,
        runtime_event_service: RuntimeEventService,
    ) -> None:
        """初始化委派生命周期 service。

        参数:
            delegation_crud: delegation 单表持久化协作者。
            runtime_event_service: runtime event 持久化与广播协作者。

        返回:
            无。

        异常:
            无。

        副作用:
            保存协作者引用。
        """

        self._delegation_crud = delegation_crud
        self._runtime_event_service = runtime_event_service

    def try_create_pending(
        self,
        *,
        task_id: str,
        parent_turn_id: str,
        parent_agent_id: str,
        child_agent_id: str,
        delegation_type: str,
        prompt: str,
        effective_tools: tuple[str, ...],
        max_concurrency: int,
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> DelegationAcquireResult:
        """按并发额度原子地尝试创建 pending delegation 并发出 started 事件。

        参数:
            task_id: 所属任务标识。
            parent_turn_id: 发起委派的父 turn 标识。
            parent_agent_id: 发起委派的父 Agent 标识。
            child_agent_id: 目标 child Agent 标识。
            delegation_type: 委派类型标签（由 child profile 的 delegation_type 派生）。
            prompt: 传给 child Agent 的任务文本；由 executor 用结构化字段拼装而成。
            effective_tools: 策略收敛后的 child 工具名称。
            max_concurrency: 同一 parent turn 下允许同时活跃（pending/running）的
                child delegation 数量上限。
            runtime_event_loop: 可选的事件循环；传入时在该循环线程安全地发布事件。

        返回:
            ``DelegationAcquireResult``：acquire 成功时 ``acquired=True`` 并携带新建
            ``delegation_id`` 与空 ``reason``；额度已满时不创建记录，返回
            ``acquired=False``、``delegation_id=""``、
            ``reason="delegation_concurrency_exceeded"``。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 持久化失败时向上抛出，由调用方收口为
            error observation；本方法不捕获持久化异常。

        副作用:
            额度未满时通过 ``DelegationCrud.create_pending_if_slot_available`` 原子地
            写入 delegations 表并尽力持久化/发布 delegation_started 事件；额度已满时
            不写记录、不发事件。并发额度校验与记录插入的原子性由 storage 层的事务
            保证（见 ``DelegationCrud.create_pending_if_slot_available``）。完成
            ``DelegationExecutor`` 调用点迁移后，本方法即成为创建 delegation 的唯一
            生产入口（届时删除 ``create_pending``）。注意：delegation_started 事件的
            持久化/发布失败会被记录为 ``delegation_event_emit_failed`` 日志后吞掉，
            不影响 acquire 结果，但事件可能丢失；调用方（executor）不应依赖该事件的可达性。
        """

        delegation_id = str(uuid4())
        record = self._build_pending_record(
            delegation_id=delegation_id,
            task_id=task_id,
            parent_turn_id=parent_turn_id,
            parent_agent_id=parent_agent_id,
            child_agent_id=child_agent_id,
            delegation_type=delegation_type,
            prompt=prompt,
            effective_tools=effective_tools,
        )
        created_id = self._delegation_crud.create_pending_if_slot_available(
            record,
            max_concurrency,
        )
        if created_id is None:
            return DelegationAcquireResult(
                acquired=False,
                delegation_id="",
                reason=REASON_CONCURRENCY_EXCEEDED,
            )
        self._emit_started(record, runtime_event_loop=runtime_event_loop)
        return DelegationAcquireResult(acquired=True, delegation_id=created_id, reason="")

    def _build_pending_record(
        self,
        *,
        delegation_id: str,
        task_id: str,
        parent_turn_id: str,
        parent_agent_id: str,
        child_agent_id: str,
        delegation_type: str,
        prompt: str,
        effective_tools: tuple[str, ...],
    ) -> DelegationRecord:
        """构造一条 pending delegation 领域记录。

        参数:
            delegation_id: 新建 delegation 的标识。
            task_id: 所属任务标识。
            parent_turn_id: 发起委派的父 turn 标识。
            parent_agent_id: 发起委派的父 Agent 标识。
            child_agent_id: 目标 child Agent 标识。
            delegation_type: 委派类型标签。
            prompt: 传给 child Agent 的任务文本。
            effective_tools: 策略收敛后的 child 工具名称。

        返回:
            状态为 ``pending``、创建/更新时间一致的 DelegationRecord。

        异常:
            无。

        副作用:
            无。
        """

        now = utc_now()
        return DelegationRecord(
            delegation_id=delegation_id,
            task_id=task_id,
            parent_turn_id=parent_turn_id,
            child_turn_id="",
            child_task_id="",
            parent_agent_id=parent_agent_id,
            child_agent_id=child_agent_id,
            delegation_type=delegation_type,
            status="pending",
            prompt=prompt,
            summary="",
            error="",
            effective_tools=effective_tools,
            created_at=now,
            updated_at=now,
        )

    def _emit_started(
        self,
        record: DelegationRecord,
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """为新建 delegation 发出 delegation_started 父事件。

        参数:
            record: 已持久化的 pending delegation 记录。
            runtime_event_loop: 可选的事件循环；传入时在该循环线程安全地发布事件。

        返回:
            无。

        异常:
            无。事件持久化或发布失败会被记录并吞掉（沿用 ``_emit_event`` 语义）。

        副作用:
            成功时写入并发布一条 delegation_started 父事件。
        """

        self._emit_event(
            record,
            EventType.DELEGATION_STARTED,
            DelegationStartedPayload(
                delegation_id=record.delegation_id,
                parent_turn_id=record.parent_turn_id,
                child_turn_id="",
                child_agent_id=record.child_agent_id,
                status="pending",
            ),
            runtime_event_loop=runtime_event_loop,
        )

    def mark_child_started(
        self,
        delegation_id: str,
        child_turn_id: str,
        child_task_id: str | None = None,
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """把 delegation 标记为 running 并发出 child_started 父事件。

        参数:
            delegation_id: 委派标识。
            child_turn_id: 已创建并开始执行的 child turn 标识。
            child_task_id: 可选的 child task 标识；传入时一并落库以便前端跳转与取消级联。
            runtime_event_loop: 父运行时事件循环；用于线程安全发布 delegation 事件。

        返回:
            无。

        异常:
            KeyError: 如果 delegation 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果 delegation 更新失败。

        副作用:
            更新 delegations 表；尽力持久化并发布 delegation_child_started 事件。
        """

        record = self._delegation_crud.update_status(
            delegation_id,
            "running",
            child_turn_id=child_turn_id,
            child_task_id=child_task_id,
        )
        self._emit_event(
            record,
            EventType.DELEGATION_CHILD_STARTED,
            DelegationChildStartedPayload(
                delegation_id=record.delegation_id,
                parent_turn_id=record.parent_turn_id,
                child_turn_id=record.child_turn_id,
                child_agent_id=record.child_agent_id,
                delegation_type=record.delegation_type,
                status="running",
            ),
            runtime_event_loop=runtime_event_loop,
        )

    def mark_completed(
        self,
        delegation_id: str,
        summary: str,
        child_task_id: str | None = None,
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """把 delegation 标记为 completed 并发出 finished 父事件。

        参数:
            delegation_id: 委派标识。
            summary: child Agent 执行摘要。
            child_task_id: 可选的 child task 标识；传入时一并落库以便前端跳转与取消级联。
            runtime_event_loop: 父运行时事件循环；用于线程安全发布 delegation 事件。

        返回:
            无。

        异常:
            KeyError: 如果 delegation 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果 delegation 更新失败。

        副作用:
            更新 delegations 表；尽力持久化并发布 delegation_finished 事件。
        """

        record = self._delegation_crud.update_status(
            delegation_id,
            "completed",
            summary=summary,
            child_task_id=child_task_id,
        )
        self._emit_event(
            record,
            EventType.DELEGATION_FINISHED,
            DelegationFinishedPayload(
                delegation_id=record.delegation_id,
                parent_turn_id=record.parent_turn_id,
                child_turn_id=record.child_turn_id,
                child_agent_id=record.child_agent_id,
                delegation_type=record.delegation_type,
                status="completed",
                summary=summary,
            ),
            runtime_event_loop=runtime_event_loop,
        )

    def mark_failed(
        self,
        delegation_id: str,
        error: str,
        child_task_id: str | None = None,
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """把 delegation 标记为 failed 并发出 failed 父事件。

        参数:
            delegation_id: 委派标识。
            error: child Agent 失败原因。
            child_task_id: 可选的 child task 标识；传入时一并落库以便前端跳转与取消级联。
            runtime_event_loop: 父运行时事件循环；用于线程安全发布 delegation 事件。

        返回:
            无。

        异常:
            KeyError: 如果 delegation 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果 delegation 更新失败。

        副作用:
            更新 delegations 表；尽力持久化并发布 delegation_failed 事件。
        """

        record = self._delegation_crud.update_status(
            delegation_id, "failed", error=error, child_task_id=child_task_id
        )
        self._emit_event(
            record,
            EventType.DELEGATION_FAILED,
            DelegationFailedPayload(
                delegation_id=record.delegation_id,
                parent_turn_id=record.parent_turn_id,
                child_turn_id=record.child_turn_id,
                child_agent_id=record.child_agent_id,
                delegation_type=record.delegation_type,
                status="failed",
                error=error,
            ),
            runtime_event_loop=runtime_event_loop,
        )

    def mark_cancelled(
        self,
        delegation_id: str,
        error: str,
        child_task_id: str | None = None,
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """把 delegation 标记为 cancelled 并发出 cancelled 父事件。

        参数:
            delegation_id: 委派标识。
            error: child Agent 取消原因。
            child_task_id: 可选的 child task 标识；传入时一并落库以便前端跳转与取消级联。
            runtime_event_loop: 父运行时事件循环；用于线程安全发布 delegation 事件。

        返回:
            无。

        异常:
            KeyError: 如果 delegation 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果 delegation 更新失败。

        副作用:
            更新 delegations 表；尽力持久化并发布 delegation_cancelled 事件。
        """

        record = self._delegation_crud.update_status(
            delegation_id, "cancelled", error=error, child_task_id=child_task_id
        )
        self._emit_event(
            record,
            EventType.DELEGATION_CANCELLED,
            DelegationCancelledPayload(
                delegation_id=record.delegation_id,
                parent_turn_id=record.parent_turn_id,
                child_turn_id=record.child_turn_id,
                child_agent_id=record.child_agent_id,
                delegation_type=record.delegation_type,
                status="cancelled",
                error=error,
            ),
            runtime_event_loop=runtime_event_loop,
        )

    def mark_interrupted_delegations_failed(self, reason: str) -> int:
        """保守落定进程中断遗留的 active delegation。

        参数:
            reason: 写入 ``error`` 字段和 failed 事件 payload 的恢复审计原因。
        返回:
            本次从 ``pending`` 或 ``running`` 标记为 ``failed`` 的 delegation 数量。
        异常:
            sqlalchemy.exc.SQLAlchemyError: 当查询或更新 delegation 记录失败时抛出。
            KeyError: 当待恢复记录在更新前被删除时由底层 ``mark_failed`` 抛出。
        副作用:
            查询 delegations 表；对每条 ``pending`` / ``running`` 记录复用 ``mark_failed`` 写入
            failed 终态和错误原因，并尽力发出 delegation_failed 父事件；写入恢复审计日志。
        """

        interrupted = self._delegation_crud.list_pending_or_running()
        for record in interrupted:
            self.mark_failed(record.delegation_id, reason)
        log.info(
            "delegation_recovery_audit_completed",
            extra={
                "msg": "委派恢复审计完成，已将中断的委派标记为失败",
                "data": {
                    "reason": reason,
                    "recovered_count": len(interrupted),
                    "delegation_ids": [record.delegation_id for record in interrupted],
                },
            },
        )
        return len(interrupted)

    def list_by_parent_turn(self, parent_turn_id: str) -> list[DelegationRecord]:
        """列出某个 parent turn 下的全部 delegation 记录。

        参数:
            parent_turn_id: parent turn 标识。
        返回:
            按创建顺序排列的 delegation 记录列表，包含 active 与终态记录。
        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询 delegation 记录失败。
        副作用:
            读取 delegations 表。
        """

        return self._delegation_crud.list_by_parent_turn(parent_turn_id)

    def list_active_by_parent_turn(self, parent_turn_id: str) -> list[DelegationRecord]:
        """列出某个 parent turn 下仍处于活动状态的 delegation。

        参数:
            parent_turn_id: parent turn 标识。

        返回:
            状态为 ``pending`` 或 ``running`` 的 delegation 记录列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询 delegation 记录失败。

        副作用:
            读取 delegations 表。
        """

        return [
            record
            for record in self._delegation_crud.list_by_parent_turn(parent_turn_id)
            if record.status in ACTIVE_DELEGATION_STATUSES
        ]

    def _emit_event(
        self,
        record: DelegationRecord,
        event_type: EventType,
        payload,
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """尽力写入并发布 parent-turn delegation 事件。

        参数:
            record: 当前 delegation 记录。
            event_type: 要发出的 runtime event 类型。
            payload: 与 event_type 匹配的 payload 值对象。

        返回:
            无。

        异常:
            无。事件持久化或发布失败会被记录并吞掉。

        副作用:
            成功时向 runtime_events 表写入一条父 turn 事件并发布到事件总线。
        """

        try:
            event = RuntimeEvent(
                event_type=event_type,
                task_id=record.task_id,
                turn_id=record.parent_turn_id,
                payload=payload,
            )
            stamped = self._runtime_event_service.save_event(event)
            if runtime_event_loop is None:
                self._runtime_event_service.publish_event(stamped)
            else:
                runtime_event_loop.call_soon_threadsafe(
                    self._runtime_event_service.publish_event,
                    stamped,
                )
        except Exception:
            log.exception(
                "delegation_event_emit_failed",
                extra={
                    "msg": "委派状态已持久化，但委派运行时事件写入或发布失败",
                    "data": {
                        "delegation_id": record.delegation_id,
                        "event_type": event_type.value,
                        "parent_turn_id": record.parent_turn_id,
                    },
                },
            )
