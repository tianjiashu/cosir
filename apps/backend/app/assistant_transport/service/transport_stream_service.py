"""Assistant Transport 帧订阅与本地 SSE 编码。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, ClassVar

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    find_run,
)
from app.assistant_transport.stream import TransportFrame
from app.assistant_transport.stream.subscriber import SubscriberClosed
from app.config.constant import Constant
from app.config.logging.logger import log


class AssistantTransportStreamService:
    """订阅单个 task 快照，并暴露项目自有的 JSON SSE 帧。

    本 service 只负责连接生命周期与 wire 编码，不启动、不恢复、不取消、不变更 run。
    首个 full 帧由 ``ConversationTaskStateService.subscribe_with_snapshot`` 在其唯一锁下准备，
    因此 attach 不会在注册与其首帧之间漏掉任何变更。
    """

    # 终态判定唯一事实源：与 fork 校验、child_task 工具共用同一份集合，禁止在本类另立副本。
    terminal_statuses: ClassVar[frozenset[str]] = Constant.Run.TERMINAL_STATUSES

    def __init__(self) -> None:
        """绑定进程本地快照持有者与 run 状态数据源。"""

        self._snapshots = ConversationTaskStateService()
        from app.service.depends import get_conversation_run_state_service

        self._runs = get_conversation_run_state_service()

    @staticmethod
    def _run(state: ConversationStateSnapshot, run_id: int) -> dict[str, Any]:
        """从 full 状态帧中返回某个 run。"""

        _, run = find_run(state, run_id)
        return run

    @staticmethod
    def _run_with_index(
            state: ConversationStateSnapshot,
            run_id: int,
    ) -> tuple[int, dict[str, Any]]:
        """从 full 状态帧中返回目标 run 的稳定数组下标与记录。"""

        index, run = find_run(state, run_id)
        return index, run

    async def stream(
        self,
        task_id: int,
        run_id: int,
        is_cancelled: Callable[[], bool],
        is_terminal: Callable[[], Awaitable[bool]] | None = None,
        poll_interval: float = 0.05,
    ) -> AsyncIterator[TransportFrame]:
        """先产出原子的 full 帧，随后产出有界的 mutation/control 帧。

        超时仅用于取消/删除/终态检查。安静的模型或工具执行会保持连接打开。当 subscriber 超出其
        有界队列时，产出 ``resync_required`` 帧并结束连接，以便客户端重新 attach 并收到新的 full 帧。
        """

        subscriber = None
        unsubscribe: Callable[[], None] | None = None
        target_status: str | None = None
        target_run_index: int | None = None
        change_count = 0
        log.info(
            "assistant_sse_stream_opened",
            extra={
                "msg": "Assistant frame SSE 状态流已打开",
                "data": {"task_id": task_id, "run_id": run_id, "poll_interval": poll_interval},
            },
        )
        try:
            subscriber, unsubscribe, first_frame = self._snapshots.subscribe_with_snapshot(task_id)
            if first_frame.state is None:
                raise RuntimeError("full first frame must include state")
            target_run_index, target_run = self._run_with_index(first_frame.state, run_id)
            target_status = target_run["status"]
            yield first_frame
            if target_status in self.terminal_statuses:
                return

            while True:
                if is_cancelled():
                    log.info(
                        "assistant_sse_client_cancelled",
                        extra={"msg": "Assistant frame SSE 检测到客户端取消", "data": {"task_id": task_id, "run_id": run_id}},
                    )
                    return
                try:
                    frame = await asyncio.wait_for(subscriber.get(), poll_interval)
                except SubscriberClosed:
                    return
                except TimeoutError:
                    if self._snapshots.is_task_deleted(task_id):
                        return
                    if is_terminal is not None and await is_terminal():
                        latest = self._snapshots.get_state(task_id)
                        target_status = self._run(latest, run_id)["status"]
                        yield self._full_frame(task_id, latest)
                        return
                    continue

                change_count += 1
                target_status = self._target_status_from_frame(
                    frame, run_id, target_status, target_run_index
                )
                if change_count <= 3 or change_count % 20 == 0 or frame.kind != "mutation":
                    log.debug(
                        "assistant_sse_frame_sampled",
                        extra={
                            "msg": "Assistant frame SSE 更新采样记录",
                            "data": {
                                "task_id": task_id,
                                "run_id": run_id,
                                "change_count": change_count,
                                "frame_kind": frame.kind,
                                "mutation_count": len(frame.mutations),
                                "target_run_status": target_status,
                            },
                        },
                    )
                yield frame
                if frame.kind == "resync_required" or target_status in self.terminal_statuses:
                    return
        except asyncio.CancelledError:
            log.warning(
                "assistant_sse_stream_cancelled",
                extra={"msg": "Assistant frame SSE 协程被取消", "data": {"task_id": task_id, "run_id": run_id}},
            )
            raise
        except Exception:
            log.exception(
                "assistant_sse_stream_failed",
                extra={"msg": "Assistant frame SSE 状态流异常结束", "data": {"task_id": task_id, "run_id": run_id}},
            )
            raise
        finally:
            if unsubscribe is not None:
                unsubscribe()
            log.info(
                "assistant_sse_stream_closed",
                extra={"msg": "Assistant frame SSE 状态流已关闭", "data": {"task_id": task_id, "run_id": run_id},},
            )

    async def stream_envelopes(
        self,
        task_id: int,
        run_id: int,
        is_cancelled: Callable[[], bool],
    ) -> AsyncIterator[str]:
        """将帧编码为紧凑的 ``data: ...`` SSE 记录。

        ``target_run_id`` 与 ``target_run_status`` 是连接级的 envelope 元数据，刻意不存入 task 快照
        状态，也从不作为版本游标使用。
        """

        target_status: str | None = None
        target_run_index: int | None = None
        async def is_terminal() -> bool:
            """仅在连接安静轮询节拍上检查目标 Run。"""

            try:
                status = self._runs.get_run(run_id).status
            except KeyError:
                return True
            return status in self.terminal_statuses

        async for frame in self.stream(task_id, run_id, is_cancelled, is_terminal=is_terminal):
            if frame.state is not None:
                target_run_index, target_run = self._run_with_index(frame.state, run_id)
                target_status = target_run["status"]
            target_status = self._target_status_from_frame(
                frame, run_id, target_status, target_run_index
            )
            payload: dict[str, Any] = {
                "task_id": frame.task_id,
                "kind": frame.kind,
                "mutations": [
                    {"kind": mutation.kind, "path": list(mutation.path), "value": mutation.value}
                    for mutation in frame.mutations
                ],
                "source_run_id": frame.source_run_id,
                "current_run_id": frame.current_run_id,
                "current_run_status": frame.current_run_status,
                "resync_reason": frame.resync_reason,
                "target_run_id": run_id,
                "target_run_status": target_status,
            }
            if frame.state is not None:
                payload["state"] = frame.state
            yield f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"

    @staticmethod
    def _full_frame(task_id: int, state: ConversationStateSnapshot) -> TransportFrame:
        """从一份已读取的状态构造 full 恢复帧。"""

        current_id = state["current_run_id"]
        current_status = next(
            (run["status"] for run in state["runs"] if run["runId"] == current_id),
            None,
        )
        return TransportFrame(
            task_id=task_id,
            kind="full",
            state=state,
            mutations=(),
            current_run_id=current_id,
            current_run_status=current_status,
        )

    def _target_status_from_frame(
        self,
        frame: TransportFrame,
        run_id: int,
        previous: str | None,
        target_run_index: int | None,
    ) -> str | None:
        """从 full 帧或其来源变更中解析目标 run 状态。"""

        if frame.state is not None:
            return self._run(frame.state, run_id)["status"]
        if frame.source_run_id == run_id:
            for mutation in frame.mutations:
                if (
                    mutation.kind == "set"
                    and target_run_index is not None
                    and mutation.path == ("runs", target_run_index, "status")
                ):
                    return mutation.value if isinstance(mutation.value, str) else previous
                if (
                    mutation.kind == "set"
                    and target_run_index is not None
                    and mutation.path == ("runs", target_run_index)
                    and isinstance(mutation.value, dict)
                ):
                    status = mutation.value.get("status")
                    return status if isinstance(status, str) else previous
        return previous


__all__ = ["AssistantTransportStreamService"]
