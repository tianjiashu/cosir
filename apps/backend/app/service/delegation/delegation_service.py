"""Delegation lifecycle service."""

import asyncio

from app.config.logging.logger import log
from app.models.delegation_record import DelegationRecord
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.payload.delegation_cancelled_payload import DelegationCancelledPayload
from app.models.payload.delegation_child_started_payload import DelegationChildStartedPayload
from app.models.payload.delegation_failed_payload import DelegationFailedPayload
from app.models.payload.delegation_finished_payload import DelegationFinishedPayload
from app.models.payload.delegation_started_payload import DelegationStartedPayload
from app.models.result.delegation_acquire_result import (
    REASON_CONCURRENCY_EXCEEDED,
    DelegationAcquireResult,
)
from app.service.agent_runtime_event.runtime_event_service import RuntimeEventService
from app.storage.crud.delegation_crud import ACTIVE_DELEGATION_STATUSES, DelegationCrud


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
            task_id: int,
            parent_turn_id: int,
            parent_agent_id: str,
            child_agent_id: str,
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
        record = DelegationRecord(
            id = None,
            task_id=task_id,
            parent_turn_id=parent_turn_id,
            child_turn_id=None,
            child_task_id=None,
            parent_agent_id=parent_agent_id,
            child_agent_id=child_agent_id,
            status="pending",
            prompt=prompt,
            summary="",
            error="",
            effective_tools=effective_tools,
        )
        created_id = self._delegation_crud.create_pending_if_slot_available(
            record,
            max_concurrency,
        )
        if created_id is None:
            return DelegationAcquireResult(
                acquired=False,
                delegation_id=-1,
                reason=REASON_CONCURRENCY_EXCEEDED,
            )
        self._emit_started(record, runtime_event_loop=runtime_event_loop)
        return DelegationAcquireResult(acquired=True, delegation_id=created_id, reason="")

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
                delegation_id=record.id,
                parent_turn_id=record.parent_turn_id,
                child_turn_id=None,
                child_agent_id=record.child_agent_id,
                status="pending",
            ),
            runtime_event_loop=runtime_event_loop,
        )

    def mark_child_started(
            self,
            delegation_id: int,
            child_turn_id: int,
            child_task_id: int | None = None,
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
                delegation_id=record.id,
                parent_turn_id=record.parent_turn_id,
                child_turn_id=record.child_turn_id,
                child_agent_id=record.child_agent_id,
                status="running",
            ),
            runtime_event_loop=runtime_event_loop,
        )

    def mark_completed(
            self,
            delegation_id: int,
            summary: str,
            child_task_id: int | None = None,
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
                delegation_id=record.id,
                parent_turn_id=record.parent_turn_id,
                child_turn_id=record.child_turn_id,
                child_agent_id=record.child_agent_id,
                status="completed",
                summary=summary,
            ),
            runtime_event_loop=runtime_event_loop,
        )

    def mark_failed(
            self,
            delegation_id: int,
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
                delegation_id=record.id,
                parent_turn_id=record.parent_turn_id,
                child_turn_id=record.child_turn_id,
                child_agent_id=record.child_agent_id,
                status="failed",
                error=error,
            ),
            runtime_event_loop=runtime_event_loop,
        )

    def mark_cancelled(
            self,
            delegation_id: int,
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
                delegation_id=record.id,
                parent_turn_id=record.parent_turn_id,
                child_turn_id=record.child_turn_id,
                child_agent_id=record.child_agent_id,
                status="cancelled",
                error=error,
            ),
            runtime_event_loop=runtime_event_loop,
        )

    def mark_interrupted_delegations_failed(self, reason: str) -> int:
        """原子落定进程中断遗留的 active delegation。

        进程级重启恢复入口（由 ``app.py`` 在启动时以 ``reason="runtime_restarted"`` 调用），
        跨全部 parent turn 恢复。恢复语义：先由数据层用单条
        ``UPDATE ... WHERE status IN (active) RETURNING id`` 原子地把残留的 ``pending`` /
        ``running`` 委派直接置为 ``failed``（消除「先 list 快照再逐条 update」的 TOCTOU，
        以及中途崩溃导致残留记录永久占用并发额度、使新委派被永久拒绝），再仅对真正被改动的
        记录补发 ``delegation_failed`` 事件。事件补发失败只记 warn 日志、不回滚已置 failed
        的数据库状态，保证额度回收不受事件通道影响。

        参数:
            reason: 写入 ``error`` 字段和 failed 事件 payload 的恢复审计原因。

        返回:
            本次实际从 ``pending`` / ``running`` 置为 ``failed`` 的 delegation 数量。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 当底层原子更新 delegation 记录失败时抛出。

        副作用:
            更新 delegations 表（仅活跃记录，跨全部 parent turn）；尽力为被改动的记录发出
            delegation_failed 父事件；写入恢复审计日志。
        """

        failed_ids = self._delegation_crud.fail_active_delegations(error=reason)
        for delegation_id in failed_ids:
            try:
                record = self._delegation_crud.get(delegation_id)
            except KeyError:
                # RETURNING 的 id 理论上必能取到记录；取不到说明已被并发清理，
                # 属异常路径但不影响额度回收，记录后跳过而非静默吞掉。
                log.warning(
                    "delegation_recovery_record_missing",
                    extra={
                        "msg": "恢复审计中 RETURNING id 对应的记录已不存在，跳过事件补发",
                        "data": {"delegation_id": delegation_id, "reason": reason},
                    },
                )
                continue
            self._emit_event(
                record,
                EventType.DELEGATION_FAILED,
                DelegationFailedPayload(
                    delegation_id=record.id,
                    parent_turn_id=record.parent_turn_id,
                    child_turn_id=record.child_turn_id,
                    child_agent_id=record.child_agent_id,
                    status="failed",
                    error=reason,
                ),
                runtime_event_loop=None,
            )
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

    def list_by_parent_turn(self, parent_turn_id: int) -> list[DelegationRecord]:
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

    def list_active_by_parent_turn(self, parent_turn_id: int) -> list[DelegationRecord]:
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
                        "delegation_id": record.id,
                        "event_type": event_type.value,
                        "parent_turn_id": record.parent_turn_id,
                    },
                },
            )
