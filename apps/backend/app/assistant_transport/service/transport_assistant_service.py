"""Assistant Transport 的 run 状态订阅适配。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, NoReturn

from assistant_stream import create_run
from assistant_stream.serialization import AssistantTransportResponse
from fastapi import HTTPException

from app.assistant_transport.service.conversation_task_snapshot_service import (
    ConversationTaskSnapshotService,
    SnapshotChange,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.config.logging.logger import log
from app.core.runtime.execution_mode import ExecutionMode
from app.models import ConversationRunStatus


class TransportAssistantService:
    """只负责把指定 run 的 canonical snapshot 订阅为 Assistant Transport 流。"""

    def __init__(self) -> None:
        """初始化 snapshot 与 run 状态查询依赖。"""

        self._snapshots = ConversationTaskSnapshotService()
        from app.service.depends import get_conversation_run_executor

        self.run_executor = get_conversation_run_executor()
        from app.service.depends import get_conversation_run_service

        self._runs = get_conversation_run_service()
        from app.service.depends import get_conversation_event_projector

        self._projector = get_conversation_event_projector()
        from app.service.depends import get_task_service

        self.task_service = get_task_service()
        from app.service.depends import get_runtime

        self.runtime = get_runtime()

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
            queue, unsubscribe = self._snapshots.subscribe(task_id)
            assert queue is not None
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
        """按 run 身份订阅任务快照并推送给 Assistant Transport controller。"""

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
                    "data": {"task_id": task_id, "run_id": run_id, "run_task_id": run.task_id},
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
            self._snapshots._apply_snapshot_change(controller, snapshot)

    async def subscribe_run_state_with_logging(
        self, controller: Any, task_id: int, run_id: int
    ) -> None:
        """在 Assistant Stream callback 边界记录完成、取消和异常。

        参数:
            controller: ``assistant-stream`` 创建的运行控制器。
            task_id: 任务标识。
            run_id: Conversation Run 标识。

        返回:
            无；``subscribe_run_state`` 正常完成时返回 ``None``。

        异常:
            ``asyncio.CancelledError`` 和其他异常原样向上传播，交由
            ``assistant-stream`` 负责正确结束 SSE。

        副作用:
            记录 SSE callback 的完整生命周期，不改变运行状态或快照事实。
        """
        log.info(
            "assistant_sse_callback_started",
            extra={
                "msg": "Assistant SSE 后台订阅 callback 已启动",
                "data": {"task_id": task_id, "run_id": run_id},
            },
        )
        try:
            await self.subscribe_run_state(controller, task_id, run_id)
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

    async def start_run(self, run_id: int, execution_mode: ExecutionMode) -> None:
        """按显式执行模式启动后台 run。

        参数:
            run_id: Conversation Run 标识。
            execution_mode: ``"fresh"`` 全新执行或 ``"resume"`` 恢复执行。

        返回:
            无。

        异常:
            ValueError: 当 run 已被其他进程内请求登记（执行器单点约束）。

        副作用:
            经 ``run_executor`` 驱动 ``runtime.execute_run`` 后台执行。
        """
        if execution_mode == "fresh":
            await self.run_executor.start(run_id, self.runtime.execute_run)
            return
        await self.run_executor.start(
            run_id,
            lambda run: self.runtime.execute_run(run, execution_mode="resume"),
        )

    async def resume_run(
        self,
        *,
        task_id: int,
        thread_id: str,
        run_id: int,
    ) -> AssistantTransportResponse:
        """
        中断续跑，只能在取消状态才能恢复。

        参数:
            task_id: 任务标识。
            thread_id: 线程标识（写入响应头 ``X-Cosir-Thread-Id``）。
            run_id: 待恢复的 Conversation Run 标识。

        返回:
            使用 ``assistant-stream`` 编码的 ``text/event-stream`` 响应。

        异常:
            HTTPException: 任务不存在、run 不可恢复、run 正在取消或冲突时抛出。

        副作用:
            如果执行器仍在本进程则只重新订阅其状态流。
        """
        try:
            self.task_service.get_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        state = await self._snapshots.read(task_id)

        latest_run = self.task_service.get_latest_run(task_id)
        if latest_run is None:
            _raise_transport_error(
                409,
                "RUN_NOT_RESUMABLE",
                "当前任务没有可恢复的运行，请读取最新快照",
                retryable=False,
                run_id=run_id,
            )

        if run_id != latest_run.id or latest_run.status != ConversationRunStatus.CANCELLED.value:
            _raise_transport_error(
                409,
                "RUN_NOT_RESUMABLE",
                "对话运行当前不可继续，请读取最新快照",
                retryable=True,
                run_id=run_id,
            )

        if self.run_executor.is_cancelling(run_id):
            _raise_transport_error(
                409,
                "RUN_CANCELLING",
                "运行正在取消，请稍后重新提交恢复请求",
                retryable=True,
                run_id=run_id,
            )
        if self.run_executor.is_locally_running(run_id):
            _raise_transport_error(
                409,
                "RUN_ALREADY_RUNNING",
                "对话运行当前正在运行，请稍后重新提交恢复请求",
                retryable=True,
                run_id=run_id,
            )
        try:
            await self.start_run(run_id, "resume")
        except ValueError:
            log.info(
                "assistant_transport_resume_executor_already_claimed",
                extra={
                    "msg": "恢复执行器已被其他请求登记，当前请求继续订阅",
                    "data": {"task_id": task_id, "run_id": run_id},
                },
            )
        stream = create_run(
            lambda controller: self.subscribe_run_state_with_logging(
                controller, task_id, run_id
            ),
            state=state,
        )
        response = AssistantTransportResponse(stream)
        response.headers["X-Cosir-Task-Id"] = str(task_id)
        response.headers["X-Cosir-Thread-Id"] = thread_id
        return response


def _raise_transport_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool,
    command_id: str | None = None,
    run_id: int | None = None,
) -> NoReturn:
    """抛出统一的 Assistant Transport HTTP 错误。

    参数:
        status_code: HTTP 状态码。
        code: 稳定的机器可读错误码。
        message: 面向用户的安全提示，不包含密钥或异常堆栈。
        retryable: 客户端是否可以在修正条件后重试。
        command_id: 可选的 Transport 命令标识。
        run_id: 可选的后端 Conversation Run 标识。

    返回:
        无；本函数始终抛出 ``HTTPException``，返回类型标注 ``NoReturn`` 供 mypy
        把所有调用点所在的 except 分支识别为不可达路径。

    异常:
        HTTPException: 携带统一 ``error`` 对象的 HTTP 异常。

    副作用:
        无。
    """
    error: dict[str, object] = {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    if command_id is not None:
        error["commandId"] = command_id
    if run_id is not None:
        error["runId"] = run_id
    raise HTTPException(status_code=status_code, detail={"error": error})
