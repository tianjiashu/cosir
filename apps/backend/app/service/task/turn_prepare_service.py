"""编排单个 turn 执行前的 CodeGraph 索引准备，并在完成后放行执行。

单一职责：接收 API 层解析好的 workspace_path 与 task_id，组合
``CodeGraphLifecycleService.ensure_ready``（同步，经 ``asyncio.to_thread`` 跑），
发布准备阶段事件（workspace_preparing / workspace_ready / workspace_degraded），
完成后回调 ``execute`` 启动 ``run_turn``。

职责边界：
- 负责：组合 ensure_ready、发布准备事件、返回「放行 or 降级」信号。
- 不负责：turn → workspace_path 解析（归 API 层 TurnWorkspaceResolver）、
  索引算法（上游 CodeGraph）、RPC（Client）、并发去重（InflightRegistry）、
  Agent 执行（run_turn 归调用方）、turn 状态持久化（归 API 层，见方案二 §九.4）。
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.config.logging.logger import log
from app.models.enums.event_type import EventType
from app.models.payload import (
    WorkspaceDegradedPayload,
    WorkspacePreparingPayload,
    WorkspaceReadyPayload,
)
from app.models.runtime_event import RuntimeEvent
from app.service.codegraph.lifecycle_service import CodeGraphLifecycleService
from app.service.runtime_event.runtime_event_service import RuntimeEventService


class TurnPrepareService:
    """编排 turn 执行前的 CodeGraph 索引准备，并在完成后放行执行。"""

    def __init__(
        self,
        lifecycle_service: CodeGraphLifecycleService,
        event_service: RuntimeEventService | None = None,
    ) -> None:
        """构造准备服务。

        参数:
            lifecycle_service: CodeGraph 索引生命周期编排服务（同步 ensure_ready）。
            event_service: 运行时事件服务（持久化 + 发布）；省略时从依赖装配取得。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """
        self._lifecycle = lifecycle_service
        self._events = event_service if event_service is not None else RuntimeEventService()

    async def prepare_then_execute(
        self,
        task_id: str,
        turn_id: str,
        workspace_path: str | None,
        execute: Callable[[], Awaitable[None]],
    ) -> None:
        """异步准备索引；就绪或降级后回调 ``execute`` 启动 ``run_turn``。

        参数:
            task_id: 所属任务标识（构造 RuntimeEvent 信封需要）。
            turn_id: 待执行的轮次标识。
            workspace_path: API 层解析好的 workspace 根路径；None 表示跳过准备。
            execute: 就绪/降级后执行的异步回调（启动 run_turn）。

        返回:
            无。

        异常:
            asyncio.CancelledError: prepare 被取消时 re-raise（由 API 层 finally 兜底）。

        副作用:
            发布准备阶段事件；可能触发 CodeGraph 索引 init/sync。
        """
        if workspace_path is None:
            log.info(
                "turn_prepare_skipped",
                extra={
                    "msg": "无 workspace 可准备，跳过 CodeGraph 索引",
                    "data": {"turn_id": turn_id},
                },
            )
            await execute()
            return

        self._emit(
            EventType.WORKSPACE_PREPARING,
            task_id,
            turn_id,
            WorkspacePreparingPayload(workspace_path=workspace_path),
        )

        # ensure_ready 是同步阻塞 RPC，放进线程池避免卡事件循环（方案二 §4.4）。
        readiness = await asyncio.to_thread(self._lifecycle.ensure_ready, workspace_path)

        if readiness.ready:
            self._emit(
                EventType.WORKSPACE_READY,
                task_id,
                turn_id,
                WorkspaceReadyPayload(
                    workspace_path=workspace_path,
                    action_taken=readiness.action_taken,  # type: ignore[arg-type]
                    files_changed=readiness.files_changed,
                    duration_ms=readiness.duration_ms,
                ),
            )
        else:
            self._emit(
                EventType.WORKSPACE_DEGRADED,
                task_id,
                turn_id,
                WorkspaceDegradedPayload(
                    workspace_path=workspace_path,
                    state=readiness.state,
                    degraded_reason=readiness.degraded_reason or "",
                ),
            )

        await execute()

    def _emit(
        self,
        event_type: EventType,
        task_id: str,
        turn_id: str,
        payload: Any,
    ) -> None:
        """持久化并发布一条运行时事件（与 run_turn 的 emit 同构）。"""
        try:
            self._events.save_and_publish(
                RuntimeEvent(
                    event_type=event_type,
                    task_id=task_id,
                    turn_id=turn_id,
                    payload=payload,
                )
            )
        except Exception:
            log.exception(
                "turn_prepare_event_emit_failed",
                extra={
                    "msg": "准备阶段事件发布失败",
                    "data": {"turn_id": turn_id, "event_type": event_type.value},
                },
            )
            # 事件发布失败不应阻断索引准备放行；仅记录并继续。
