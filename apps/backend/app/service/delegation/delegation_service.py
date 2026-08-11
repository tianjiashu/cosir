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
from app.storage.crud.delegation_crud import DelegationCrud
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

    def count_active_children(self, parent_turn_id: str) -> int:
        """统计某 parent turn 下仍活跃的 child delegation 数量。

        参数:
            parent_turn_id: 父 turn 标识。

        返回:
            状态为 ``pending`` 或 ``running`` 的 delegation 数量。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            读取 delegations 表。
        """

        return sum(
            1
            for record in self._delegation_crud.list_by_parent_turn(parent_turn_id)
            if record.status in {"pending", "running"}
        )

    def create_pending(
        self,
        task_id: str,
        parent_turn_id: str,
        parent_agent_id: str,
        child_agent_id: str,
        delegation_type: str,
        prompt: str,
        requested_tools: tuple[str, ...],
        effective_tools: tuple[str, ...],
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> str:
        """创建 pending delegation 并发出 delegation_started 父事件。

        参数:
            task_id: 所属任务标识。
            parent_turn_id: 发起委派的父 turn 标识。
            parent_agent_id: 发起委派的父 Agent 标识。
            child_agent_id: 目标 child Agent 标识。
            delegation_type: 委派类型标签。
            prompt: 传给 child Agent 的任务指令。
            requested_tools: 请求开放给 child 的工具名称。
            effective_tools: 策略收敛后的 child 工具名称。

        返回:
            新建 delegation 的标识。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果 delegation 持久化失败。

        副作用:
            写入 delegations 表；尽力持久化并发布 delegation_started 事件。
        """

        now = utc_now()
        delegation_id = str(uuid4())
        record = DelegationRecord(
            delegation_id=delegation_id,
            task_id=task_id,
            parent_turn_id=parent_turn_id,
            child_turn_id="",
            parent_agent_id=parent_agent_id,
            child_agent_id=child_agent_id,
            delegation_type=delegation_type,
            status="pending",
            prompt=prompt,
            summary="",
            error="",
            requested_tools=requested_tools,
            effective_tools=effective_tools,
            created_at=now,
            updated_at=now,
        )
        self._delegation_crud.create(record)
        self._emit_event(
            record,
            EventType.DELEGATION_STARTED,
            DelegationStartedPayload(
                delegation_id=delegation_id,
                parent_turn_id=parent_turn_id,
                child_turn_id="",
                child_agent_id=child_agent_id,
                delegation_type=delegation_type,
                status="pending",
            ),
            runtime_event_loop=runtime_event_loop,
        )
        return delegation_id

    def mark_child_started(
        self,
        delegation_id: str,
        child_turn_id: str,
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """把 delegation 标记为 running 并发出 child_started 父事件。

        参数:
            delegation_id: 委派标识。
            child_turn_id: 已创建并开始执行的 child turn 标识。

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
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """把 delegation 标记为 completed 并发出 finished 父事件。

        参数:
            delegation_id: 委派标识。
            summary: child Agent 执行摘要。

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
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """把 delegation 标记为 failed 并发出 failed 父事件。

        参数:
            delegation_id: 委派标识。
            error: child Agent 失败原因。

        返回:
            无。

        异常:
            KeyError: 如果 delegation 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果 delegation 更新失败。

        副作用:
            更新 delegations 表；尽力持久化并发布 delegation_failed 事件。
        """

        record = self._delegation_crud.update_status(delegation_id, "failed", error=error)
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
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """把 delegation 标记为 cancelled 并发出 cancelled 父事件。

        参数:
            delegation_id: 委派标识。
            error: child Agent 取消原因。

        返回:
            无。

        异常:
            KeyError: 如果 delegation 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果 delegation 更新失败。

        副作用:
            更新 delegations 表；尽力持久化并发布 delegation_cancelled 事件。
        """

        record = self._delegation_crud.update_status(delegation_id, "cancelled", error=error)
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
            if record.status in {"pending", "running"}
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
