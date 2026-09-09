"""Assistant Transport 的 run 快照订阅与 SSE 编码。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.assistant_transport.service.conversation_task_snapshot_service import (
    ConversationTaskSnapshotService,
    SnapshotChange,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.config.logging.logger import log
from app.models import ConversationRunStatus


class AssistantTransportStreamService:
    """把指定 run 的 canonical snapshot 订阅并编码为 Assistant Transport SSE 流。

    仅负责快照订阅与编码；run 的前置校验、生命周期编排与响应构造由
    ``TransportAssistantService`` 负责，并通过委托本类完成流式输出。
    """

    def __init__(self) -> None:
        """初始化 snapshot 订阅与 run 状态查询依赖。"""

        self._snapshots = ConversationTaskSnapshotService()
        from app.service.depends import get_conversation_run_service

        self._runs = get_conversation_run_service()
        from app.service.depends import get_conversation_run_executor

        self.run_executor = get_conversation_run_executor()

    async def stream(
        self,
        task_id: int,
        run_id: int,
        is_cancelled: Callable[[], bool],
        is_terminal: Callable[[], Awaitable[bool]] | None = None,
        poll_interval: float = 0.05,
    ) -> AsyncIterator[SnapshotChange]:
        """订阅 run 的 snapshot，直到终态、客户端取消或后端关闭。

        ``poll_interval`` 只用于在没有队列通知时检查取消和数据库终态，绝不代表
        stream 的生命周期。模型思考、工具执行或网络等待超过任意时长，都不能让流
        被当作已完成而关闭。

        参数:
            task_id: 订阅所属任务。
            run_id: 订阅所属 run。
            is_cancelled: 客户端连接取消判定。
            is_terminal: 可选的数据库/执行器终态查询。
            poll_interval: 取消与终态轮询间隔。

        返回:
            一个按 canonical snapshot change 顺序产生状态更新的异步迭代器。

        异常:
            asyncio.CancelledError: 后端进程关闭或请求协程被取消时向上传播，交由
                Assistant Transport 关闭响应。

        副作用:
            注册并最终注销一个任务级 snapshot subscriber；不会启动、取消或修改 run。
        """

        queue: asyncio.Queue[SnapshotChange] | None = None
        unsubscribe: Callable[[], None] | None = None
        terminal_statuses = {
            ConversationRunStatus.COMPLETED.value,
            ConversationRunStatus.FAILED.value,
            ConversationRunStatus.CANCELLED.value,
            ConversationRunStatus.INTERRUPTED.value,
        }
        change_count = 0
        log.info(
            "assistant_sse_stream_opened",
            extra={
                "msg": "Assistant SSE 状态流已打开",
                "data": {"task_id": task_id, "run_id": run_id, "poll_interval": poll_interval},
            },
        )
        try:
            subscribe_with_snapshot = getattr(self._snapshots, "subscribe_with_snapshot", None)
            if callable(subscribe_with_snapshot):
                queue, unsubscribe, initial = subscribe_with_snapshot(task_id)
            else:
                # 保留对轻量测试替身/旧注入实现的兼容；生产 snapshot service 使用
                # subscribe_with_snapshot 保证首帧与订阅注册的原子顺序。
                queue, unsubscribe = self._snapshots.subscribe(task_id)
                initial = self._snapshots.ensure_state_snapshot(task_id)
            log.info(
                "assistant_sse_initial_snapshot_prepared",
                extra={
                    "msg": "Assistant SSE 首帧快照已准备",
                    "data": {
                        "task_id": task_id,
                        "run_id": run_id,
                        "snapshot_run_id": initial["run"]["runId"],
                        "run_status": initial["run"]["status"],
                        "message_count": len(initial["messages"]),
                        "mutation_count": 1,
                        "mutation_types": ["set"],
                    },
                },
            )
            if initial["run"]["runId"] != run_id:
                log.warning(
                    "assistant_sse_initial_snapshot_run_mismatch",
                    extra={
                        "msg": "Assistant SSE 首帧快照不属于请求的 run，拒绝发送",
                        "data": {
                            "task_id": task_id,
                            "expected_run_id": run_id,
                            "actual_run_id": initial["run"]["runId"],
                        },
                    },
                )
                raise ValueError(
                    f"snapshot run {initial['run']['runId']} does not match requested run {run_id}"
                )
            yield SnapshotChange(
                task_id,
                initial,
                (ConversationStateMutation("set", (), initial),),
            )

            if initial["run"]["runId"] == run_id and initial["run"]["status"] in terminal_statuses:
                log.info(
                    "assistant_sse_initial_snapshot_terminal",
                    extra={
                        "msg": "Assistant SSE 首帧已经是终态，正常结束",
                        "data": {
                            "task_id": task_id,
                            "run_id": run_id,
                            "run_status": initial["run"]["status"],
                        },
                    },
                )
                return

            while True:
                if is_cancelled():
                    log.warning(
                        "assistant_sse_client_cancelled",
                        extra={
                            "msg": "Assistant SSE 检测到客户端取消",
                            "data": {"task_id": task_id, "run_id": run_id},
                        },
                    )
                    return

                try:
                    change = await asyncio.wait_for(queue.get(), poll_interval)
                except TimeoutError:
                    is_task_deleted = getattr(self._snapshots, "is_task_deleted", None)
                    if callable(is_task_deleted) and is_task_deleted(task_id):
                        log.info(
                            "assistant_sse_task_deleted",
                            extra={
                                "msg": "Assistant SSE 因 task 删除而结束",
                                "data": {"task_id": task_id, "run_id": run_id},
                            },
                        )
                        return
                    if is_terminal is not None and await is_terminal():
                        latest = self._snapshots.ensure_state_snapshot(task_id)
                        if (
                            latest["run"]["runId"] == run_id
                            and latest["run"]["status"] in terminal_statuses
                        ):
                            log.info(
                                "assistant_sse_terminal_snapshot_prepared",
                                extra={
                                    "msg": "Assistant SSE 轮询发现终态快照",
                                    "data": {
                                        "task_id": task_id,
                                        "run_id": run_id,
                                        "run_status": latest["run"]["status"],
                                        "message_count": len(latest["messages"]),
                                        "mutation_count": 1,
                                        "mutation_types": ["set"],
                                    },
                                },
                            )
                            yield SnapshotChange(
                                task_id,
                                latest,
                                (ConversationStateMutation("set", (), latest),),
                            )
                            return
                    # 暂时没有 mutation 不是 run 结束。保持 SSE 打开。
                    continue

                if change.state["run"]["runId"] != run_id:
                    log.warning(
                        "assistant_sse_run_mismatch",
                        extra={
                            "msg": "Assistant SSE 收到不属于当前 run 的快照，停止订阅",
                            "data": {
                                "task_id": task_id,
                                "expected_run_id": run_id,
                                "actual_run_id": change.state["run"]["runId"],
                                "actual_run_status": change.state["run"]["status"],
                            },
                        },
                    )
                    return
                change_count += 1
                if (
                    change_count <= 3
                    or change_count % 20 == 0
                    or change.state["run"]["status"] in terminal_statuses
                ):
                    log.debug(
                        "assistant_sse_snapshot_change_sampled",
                        extra={
                            "msg": "Assistant SSE 状态更新采样记录",
                            "data": {
                                "task_id": task_id,
                                "run_id": run_id,
                                "change_count": change_count,
                                "run_status": change.state["run"]["status"],
                                "message_count": len(change.state["messages"]),
                                "mutation_count": len(change.mutations),
                                "mutation_types": [mutation.kind for mutation in change.mutations],
                            },
                        },
                    )
                yield change
                if change.state["run"]["status"] in terminal_statuses:
                    log.info(
                        "assistant_sse_stream_terminal",
                        extra={
                            "msg": "Assistant SSE 已发送终态并正常结束",
                            "data": {
                                "task_id": task_id,
                                "run_id": run_id,
                                "run_status": change.state["run"]["status"],
                            },
                        },
                    )
                    return
        except asyncio.CancelledError:
            log.warning(
                "assistant_sse_stream_cancelled",
                extra={
                    "msg": "Assistant SSE 协程被取消",
                    "data": {"task_id": task_id, "run_id": run_id},
                },
            )
            raise
        except Exception:
            log.exception(
                "assistant_sse_stream_failed",
                extra={
                    "msg": "Assistant SSE 状态流异常结束",
                    "data": {"task_id": task_id, "run_id": run_id},
                },
            )
            raise
        finally:
            if unsubscribe is not None:
                unsubscribe()
            log.info(
                "assistant_sse_stream_closed",
                extra={
                    "msg": "Assistant SSE 状态流已关闭",
                    "data": {"task_id": task_id, "run_id": run_id},
                },
            )

    async def subscribe_run_state(
        self,
        controller: Any,
        task_id: int,
        run_id: int,
    ) -> None:
        """按 run 身份订阅任务快照并推送给 Assistant Transport controller。

        同时记录 SSE callback 的完整生命周期（启动/取消/完成/失败），不改变
        运行状态或快照事实。
        """

        log.info(
            "assistant_sse_callback_started",
            extra={
                "msg": "Assistant SSE 后台订阅 callback 已启动",
                "data": {"task_id": task_id, "run_id": run_id},
            },
        )
        try:
            try:
                run = self._runs.get_run(run_id)
            except Exception:
                log.exception(
                    "assistant_sse_subscription_lookup_failed",
                    extra={
                        "msg": "Assistant SSE 订阅前读取 run 失败",
                        "data": {"task_id": task_id, "run_id": run_id},
                    },
                )
                raise
            if run.task_id != task_id:
                log.warning(
                    "assistant_sse_subscription_rejected",
                    extra={
                        "msg": "Assistant SSE 订阅被拒绝，run 不属于 task",
                        "data": {
                            "task_id": task_id,
                            "run_id": run_id,
                            "run_task_id": run.task_id,
                        },
                    },
                )
                raise ValueError(f"run {run_id} does not belong to task {task_id}")
            log.info(
                "assistant_sse_subscription_started",
                extra={
                    "msg": "Assistant SSE 开始订阅 run",
                    "data": {"task_id": task_id, "run_id": run_id, "run_task_id": run.task_id},
                },
            )

            async def is_terminal() -> bool:
                """返回 run 是否已进入终态。"""

                status = await self.run_executor.status(run_id)
                if status is None:
                    snapshot = self._snapshots.ensure_state_snapshot(task_id)
                    return snapshot["run"]["status"] in {
                        ConversationRunStatus.COMPLETED.value,
                        ConversationRunStatus.FAILED.value,
                        ConversationRunStatus.CANCELLED.value,
                        ConversationRunStatus.INTERRUPTED.value,
                    }
                return status.status in {
                    ConversationRunStatus.COMPLETED,
                    ConversationRunStatus.FAILED,
                    ConversationRunStatus.CANCELLED,
                    ConversationRunStatus.INTERRUPTED,
                }

            async for snapshot in self.stream(
                task_id,
                run_id,
                lambda: controller.is_cancelled,
                is_terminal=is_terminal,
            ):
                self._apply_snapshot_change(controller, snapshot)
        except asyncio.CancelledError:
            log.warning(
                "assistant_sse_callback_cancelled",
                extra={
                    "msg": "Assistant SSE 后台订阅 callback 被取消",
                    "data": {"task_id": task_id, "run_id": run_id},
                },
            )
            raise
        except Exception:
            log.exception(
                "assistant_sse_callback_failed",
                extra={
                    "msg": "Assistant SSE 后台订阅 callback 执行失败",
                    "data": {"task_id": task_id, "run_id": run_id},
                },
            )
            raise
        else:
            log.info(
                "assistant_sse_callback_finished",
                extra={
                    "msg": "Assistant SSE 后台订阅 callback 已正常结束",
                    "data": {"task_id": task_id, "run_id": run_id},
                },
            )

    def _apply_snapshot_change(self, controller: Any, change: SnapshotChange) -> None:
        """把 canonical snapshot mutation 编码到 Assistant Stream controller。"""

        try:
            for mutation in change.mutations:
                self._apply_mutation(controller, mutation)
            controller.flush()
        except Exception:
            log.exception(
                "assistant_sse_controller_flush_failed",
                extra={
                    "msg": "Assistant SSE controller 刷新状态更新失败",
                    "data": {
                        "task_id": change.task_id,
                        "run_id": change.state["run"]["runId"],
                        "run_status": change.state["run"]["status"],
                        "mutation_count": len(change.mutations),
                        "mutation_types": [mutation.kind for mutation in change.mutations],
                    },
                },
            )
            raise

        flush_count = getattr(self, "_flush_log_count", 0) + 1
        self._flush_log_count = flush_count
        if (
            flush_count <= 3
            or flush_count % 20 == 0
            or change.state["run"]["status"]
            in {
                ConversationRunStatus.COMPLETED.value,
                ConversationRunStatus.FAILED.value,
                ConversationRunStatus.CANCELLED.value,
                ConversationRunStatus.INTERRUPTED.value,
            }
        ):
            log.debug(
                "assistant_sse_controller_flushed",
                extra={
                    "msg": "Assistant SSE controller 已刷新状态更新（采样）",
                    "data": {
                        "task_id": change.task_id,
                        "run_id": change.state["run"]["runId"],
                        "run_status": change.state["run"]["status"],
                        "flush_count": flush_count,
                        "message_count": len(change.state["messages"]),
                        "mutation_count": len(change.mutations),
                        "mutation_types": [mutation.kind for mutation in change.mutations],
                    },
                },
            )

    @staticmethod
    def _apply_mutation(controller: Any, mutation: ConversationStateMutation) -> None:
        """把单个 snapshot mutation 应用到 Assistant Stream controller。"""

        if not mutation.path:
            if mutation.kind != "set":
                raise TypeError("root snapshot mutation must use kind='set'")
            controller.state = mutation.value
            return
        if mutation.kind == "append-text":
            if not isinstance(mutation.value, str):
                raise TypeError(
                    "append-text snapshot mutation value must be str, "
                    f"got {type(mutation.value).__name__}"
                )
            controller.append_state_text(list(mutation.path), mutation.value)
            return
        target: Any = controller.state
        for key in mutation.path[:-1]:
            target = target[key]
        key = mutation.path[-1]
        if (
            isinstance(key, int)
            and key == len(target)
            and callable(getattr(target, "append", None))
        ):
            target.append(mutation.value)
            return
        target[key] = mutation.value
