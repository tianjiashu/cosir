"""进程内 ConversationRun 后台执行器。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from app.config.logging.logger import log
from app.models import ConversationRunRecord, ConversationRunStatus
from app.service.depends import get_conversation_run_service
from app.assistant_transport.service.conversation_mutation_writer import ConversationMutationWriter

ConversationRunRunner = Callable[[ConversationRunRecord], Awaitable[None]]


class ConversationRunExecutionPort(Protocol):
    """执行器所需的 ConversationRun 服务端口。"""

    def get_run(self, run_id: int) -> ConversationRunRecord:
        """读取运行记录。"""

    def claim_pending_run(self, run_id: int) -> bool:
        """原子启动 pending run。"""


@dataclass(frozen=True)
class ConversationRunState:
    """进程内运行状态的不可变快照。"""

    run_id: int
    status: ConversationRunStatus


@dataclass
class _Execution:
    """执行器内部维护的运行条目。"""

    state: ConversationRunState
    task: asyncio.Task[None]


class ConversationRunExecutor:
    """在进程内独立驱动一次 ConversationRun。"""

    def __init__(
        self,
        mutation_writer: ConversationMutationWriter | None = None,
        run_service: ConversationRunExecutionPort | None = None,
    ) -> None:
        """初始化执行器及其进程内运行注册表。"""
        self._run_service = (
            run_service if run_service is not None else get_conversation_run_service()
        )
        self._mutation_writer = mutation_writer
        self._lock = asyncio.Lock()
        self._task_locks: dict[int, asyncio.Lock] = {}
        self._executions: dict[int, _Execution] = {}

    async def start(self, run_id: int, runner: ConversationRunRunner) -> asyncio.Task[None]:
        """原子启动 run 并创建独立后台 task。

        参数:
            run_id: 运行对应的 ``ConversationRunRecord.id``。
            runner: ``AgentRuntime.execute_run`` 或同签名适配器。

        返回:
            已创建的后台 ``asyncio.Task``。

        异常:
            KeyError: ``run_id`` 对应的 run 不存在。
            ValueError: 该 run 已有未结束的后台执行。

        副作用:
            在当前事件循环创建后台 task；HTTP 订阅断开不会取消它。
        """

        run = self._run_service.get_run(run_id)
        async with self._lock:
            existing = self._executions.get(run_id)
            if existing is not None and not existing.task.done():
                raise ValueError(f"run {run_id} is already executing")
            if not self._run_service.claim_pending_run(run_id):
                raise ValueError(f"run {run_id} was claimed by another executor")
            run = self._run_service.get_run(run_id)
            state = ConversationRunState(run_id, ConversationRunStatus.PENDING)
            task = asyncio.create_task(self._execute(run_id, run, runner))
            self._executions[run_id] = _Execution(state, task)
            return task

    async def close(self) -> None:
        """优雅关闭执行器并取消活动执行。"""
        async with self._lock:
            executions = list(self._executions.values())
        for execution in executions:
            if not execution.task.done():
                execution.task.cancel()
        if executions:
            await asyncio.gather(
                *(execution.task for execution in executions), return_exceptions=True
            )

    async def cancel(self, run_id: int) -> bool:
        """显式取消一个活动 run。

        参数:
            run_id: 待取消的运行标识。

        返回:
            成功向活动 task 发出取消请求时为 ``True``；未知或已结束 run 为 ``False``。

        异常:
            无。取消请求是幂等的。

        副作用:
            向目标 asyncio task 发出取消请求；HTTP 订阅断开不会调用此方法。
        """

        async with self._lock:
            execution = self._executions.get(run_id)
            if execution is None or execution.task.done():
                return False
            execution.state = ConversationRunState(run_id, ConversationRunStatus.CANCELLED)
            execution.task.cancel()
            return True

    async def get_status(self, run_id: int) -> ConversationRunState | None:
        """查询一个 run 的当前进程内状态快照。

        参数:
            run_id: 运行标识。

        返回:
            已知 run 的不可变状态快照；未知 run 返回 ``None``。

        异常:
            无。

        副作用:
            无。
        """

        async with self._lock:
            execution = self._executions.get(run_id)
            return None if execution is None else execution.state

    async def status(self, run_id: int) -> ConversationRunState | None:
        """查询运行状态；这是 ``get_status`` 的简短别名。

        参数:
            run_id: 运行标识。

        返回:
            与 ``get_status`` 相同的状态快照或 ``None``。

        异常:
            无。

        副作用:
            无。
        """

        return await self.get_status(run_id)

    async def _execute(
        self,
        run_id: int,
        run: ConversationRunRecord,
        runner: ConversationRunRunner,
    ) -> None:
        """执行 runner、消费事件生成器并收束状态。

        同 task 的多次 run 经进程内 per-task 锁串行，确保「同一 task 内一次只有
        一个 running run」（不同 task 之间仍可并发），与桌面端并发语义一致。锁在
        后台 task 内持有，端点 ``start`` 立即返回 SSE 首帧，不被阻塞。
        """

        await self._set_status(run_id, ConversationRunStatus.RUNNING)
        async with self._lock:
            task_lock = self._task_locks.setdefault(run.task_id, asyncio.Lock())
        await task_lock.acquire()
        try:
            try:
                await runner(run)
                if self._mutation_writer is not None:
                    self._mutation_writer.settle_run(
                        run_id,
                        "completed",
                    )
                await self._set_status(run_id, ConversationRunStatus.COMPLETED)
            except asyncio.CancelledError:
                if self._mutation_writer is not None:
                    self._mutation_writer.settle_open_tool_calls(
                        run_id, "cancelled", "executor_cancelled"
                    )
                    self._mutation_writer.settle_run(
                        run_id,
                        "cancelled",
                        end_reason="executor_cancelled",
                    )
                await self._set_status(run_id, ConversationRunStatus.CANCELLED)
                raise
            except Exception:
                if self._mutation_writer is not None:
                    self._mutation_writer.settle_open_tool_calls(run_id, "failed", "runtime_failed")
                    self._mutation_writer.settle_run(
                        run_id,
                        "failed",
                        end_reason="runtime_failed",
                    )
                await self._set_status(run_id, ConversationRunStatus.FAILED)
                log.exception(
                    "conversation_run_failed",
                    extra={"msg": "后台 ConversationRun 执行失败", "data": {"run_id": run_id}},
                )
        finally:
            task_lock.release()
            current = self._executions.get(run_id)
            if current is not None and current.task is asyncio.current_task():
                self._executions.pop(run_id, None)

    async def _set_status(self, run_id: int, status: ConversationRunStatus) -> None:
        """在锁内更新指定 run 的状态。"""

        async with self._lock:
            execution = self._executions.get(run_id)
            if execution is not None:
                execution.state = ConversationRunState(run_id, status)
