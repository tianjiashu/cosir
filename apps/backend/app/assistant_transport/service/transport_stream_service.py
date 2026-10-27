"""Assistant Transport 帧订阅与本地 SSE 编码（Task 级长订阅）。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.stream import TransportFrame
from app.assistant_transport.stream.subscriber import SubscriberClosed
from app.config.logging.logger import log


class AssistantTransportStreamService:
    """订阅单个 Task 的快照，并暴露项目自有的 JSON SSE 帧。

    订阅跟随 Task，不绑定某个 Run：Task 还没有 Run 时照样推 full 帧并等待；Run 完成、失败、
    取消或挂起都不结束连接，最新 Run 变化只体现在帧里的 ``activity``。连接只在客户端断开、
    ``resync_required``（背压）或 Task 删除时结束。本 service 只负责连接生命周期与 wire 编码，
    不启动、不恢复、不取消、不变更 Run。首个 full 帧由
    ``ConversationTaskStateService.subscribe_with_snapshot`` 在其唯一锁下准备，因此 attach 不会在
    注册与其首帧之间漏掉任何变更。
    """

    def __init__(self) -> None:
        self._snapshots = ConversationTaskStateService()

    async def stream(
        self,
        task_id: int,
        is_cancelled: Callable[[], bool],
        poll_interval: float = 0.05,
    ) -> AsyncIterator[TransportFrame]:
        """先产出原子的 full 帧，随后持续产出 mutation / control 帧。

        超时只用于检查客户端取消与 Task 删除；安静的模型、工具执行或空闲的 Task 都保持连接。
        subscriber 超出其有界队列时产出 ``resync_required`` 帧并结束连接，客户端重新 attach
        取得新的 full 帧。
        """

        unsubscribe: Callable[[], None] | None = None
        change_count = 0
        log.info(
            "assistant_sse_stream_opened",
            extra={
                "msg": "Assistant frame SSE 状态流已打开",
                "data": {"task_id": task_id, "poll_interval": poll_interval},
            },
        )
        try:
            subscriber, unsubscribe, first_frame = self._snapshots.subscribe_with_snapshot(task_id)
            yield first_frame
            while True:
                if is_cancelled():
                    log.info(
                        "assistant_sse_client_cancelled",
                        extra={
                            "msg": "Assistant frame SSE 检测到客户端取消",
                            "data": {"task_id": task_id},
                        },
                    )
                    return
                try:
                    frame = await asyncio.wait_for(subscriber.get(), poll_interval)
                except SubscriberClosed:
                    return
                except TimeoutError:
                    if self._snapshots.is_task_deleted(task_id):
                        return
                    continue
                change_count += 1
                if change_count <= 3 or change_count % 20 == 0 or frame.kind != "mutation":
                    log.debug(
                        "assistant_sse_frame_sampled",
                        extra={
                            "msg": "Assistant frame SSE 更新采样记录",
                            "data": {
                                "task_id": task_id,
                                "change_count": change_count,
                                "frame_kind": frame.kind,
                                "mutation_count": len(frame.mutations),
                            },
                        },
                    )
                yield frame
                if frame.kind == "resync_required":
                    return
        except asyncio.CancelledError:
            log.warning(
                "assistant_sse_stream_cancelled",
                extra={"msg": "Assistant frame SSE 协程被取消", "data": {"task_id": task_id}},
            )
            raise
        except Exception:
            log.exception(
                "assistant_sse_stream_failed",
                extra={"msg": "Assistant frame SSE 状态流异常结束", "data": {"task_id": task_id}},
            )
            raise
        finally:
            if unsubscribe is not None:
                unsubscribe()
            log.info(
                "assistant_sse_stream_closed",
                extra={
                    "msg": "Assistant frame SSE 状态流已关闭",
                    "data": {"task_id": task_id, "change_count": change_count},
                },
            )

    async def stream_envelopes(
        self, task_id: int, is_cancelled: Callable[[], bool]
    ) -> AsyncIterator[str]:
        """将帧编码为紧凑的 ``data: ...`` SSE 记录。"""

        async for frame in self.stream(task_id, is_cancelled):
            payload: dict[str, Any] = {
                "task_id": frame.task_id,
                "kind": frame.kind,
                "mutations": [
                    {"kind": mutation.kind, "path": list(mutation.path), "value": mutation.value}
                    for mutation in frame.mutations
                ],
                "resync_reason": frame.resync_reason,
            }
            if frame.state is not None:
                payload["state"] = frame.state
            yield f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


__all__ = ["AssistantTransportStreamService"]
