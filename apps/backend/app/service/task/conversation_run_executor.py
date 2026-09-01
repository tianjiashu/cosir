"""进程内 ConversationRun 后台执行器。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import uuid4

from app.config.logging.logger import log
from app.models import TurnRecord
from app.service.task.conversation_command_service import ConversationCommandService
from app.service.task.conversation_mutation_writer import ConversationMutationWriter

ConversationRunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


class TurnReader(Protocol):
    """读取运行所需 ``TurnRecord`` 的最小端口。"""

    def get_turn(self, turn_id: int) -> TurnRecord:
        """按 turn id 读取轮次记录。"""


class LeaseClaimingTurnReader(TurnReader, Protocol):
    """同时提供持久化执行租约认领的最小端口。"""

    def claim_executor_lease(
        self, turn_id: int, owner: str, lease_seconds: int = 60
    ) -> TurnRecord | None:
        """条件认领 pending turn 并返回新的 fencing 快照。"""

    def release_executor_lease(self, turn_id: int, owner: str, fencing_version: int) -> bool:
        """在执行结束后释放当前 lease。"""


ConversationRunRunner = Callable[[TurnRecord], Awaitable[None]]


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
    owner: str | None = None
    fencing_version: int = 0


class ConversationRunExecutor:
    """在进程内独立驱动 ``AgentRuntime.run_turn`` 风格的 runner。"""

    def __init__(
        self,
        turn_reader: TurnReader,
        mutation_writer: ConversationMutationWriter | None = None,
        command_service: ConversationCommandService | None = None,
    ) -> None:
        """初始化执行器及其进程内运行注册表。"""

        self._turn_reader = turn_reader
        self._mutation_writer = mutation_writer
        self._command_service = command_service
        self._lock = asyncio.Lock()
        self._executions: dict[int, _Execution] = {}

    async def start(self, run_id: int, runner: ConversationRunRunner) -> asyncio.Task[None]:
        """读取 turn 并创建独立后台 task。

        参数:
            run_id: 运行对应的 ``TurnRecord.id``。
            runner: ``AgentRuntime.run_turn`` 或同签名适配器。

        返回:
            已创建的后台 ``asyncio.Task``。

        异常:
            KeyError: ``run_id`` 对应的 turn 不存在。
            ValueError: 该 run 已有未结束的后台执行。

        副作用:
            在当前事件循环创建后台 task；HTTP 订阅断开不会取消它。
        """

        turn = self._turn_reader.get_turn(run_id)
        async with self._lock:
            existing = self._executions.get(run_id)
            if existing is not None and not existing.task.done():
                raise ValueError(f"run {run_id} is already executing")
            owner = f"executor-{uuid4().hex}"
            claim = getattr(self._turn_reader, "claim_executor_lease", None)
            if callable(claim):
                leased_turn = claim(run_id, owner)
                if leased_turn is None:
                    raise ValueError(f"run {run_id} was claimed by another executor")
                turn = leased_turn
            state = ConversationRunState(run_id, "pending")
            task = asyncio.create_task(self._execute(run_id, turn, runner))
            self._executions[run_id] = _Execution(
                state, task, owner if callable(claim) else None, turn.fencing_version
            )
            return task

    async def recover(self, runner: ConversationRunRunner) -> int:
        """在应用启动时认领并恢复所有 pending/lease 过期的运行。"""
        list_recoverable = getattr(self._turn_reader, "list_recoverable", None)
        if not callable(list_recoverable):
            return 0
        recovered = 0
        for turn in list_recoverable():
            if turn.workflow_version != "react_like_v1":
                if self._mutation_writer is not None:
                    self._mutation_writer.settle_run(
                        turn.id,
                        "failed",
                        end_reason="runtime_recovery_failed",
                        fencing_version=turn.fencing_version,
                    )
                continue
            try:
                await self.start(turn.id, runner)
            except ValueError:
                continue
            recovered += 1
        return recovered

    async def close(self) -> None:
        """优雅关闭执行器，取消活动执行并释放其 lease。"""
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
            execution.state = ConversationRunState(run_id, "cancelled")
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

    async def _execute(self, run_id: int, turn: TurnRecord, runner: ConversationRunRunner) -> None:
        """执行 runner、消费事件生成器并收束状态。"""

        await self._set_status(run_id, "running")
        renewal: asyncio.Task[None] | None = None
        execution = self._executions.get(run_id)
        if execution is not None and execution.owner is not None:
            renewal = asyncio.create_task(
                self._renew_lease(run_id, execution.owner, turn.fencing_version)
            )
        try:
            await runner(turn)
            if self._mutation_writer is not None:
                self._mutation_writer.settle_run(
                    run_id,
                    "completed",
                    fencing_version=turn.fencing_version,
                )
            await self._set_status(run_id, "completed")
        except asyncio.CancelledError:
            if self._mutation_writer is not None:
                self._mutation_writer.settle_open_tool_calls(
                    run_id, "cancelled", "executor_cancelled", turn.fencing_version
                )
                self._mutation_writer.settle_run(
                    run_id,
                    "cancelled",
                    end_reason="executor_cancelled",
                    fencing_version=turn.fencing_version,
                )
            await self._set_status(run_id, "cancelled")
            raise
        except Exception:
            if self._mutation_writer is not None:
                self._mutation_writer.settle_open_tool_calls(
                    run_id, "failed", "runtime_failed", turn.fencing_version
                )
                self._mutation_writer.settle_run(
                    run_id,
                    "failed",
                    end_reason="runtime_failed",
                    fencing_version=turn.fencing_version,
                )
            await self._set_status(run_id, "failed")
            log.exception(
                "conversation_run_failed",
                extra={"msg": "后台 ConversationRun 执行失败", "data": {"run_id": run_id}},
            )
        finally:
            if renewal is not None:
                renewal.cancel()
            execution = self._executions.get(run_id)
            release = getattr(self._turn_reader, "release_executor_lease", None)
            if execution is not None and execution.owner is not None and callable(release):
                release(run_id, execution.owner, turn.fencing_version)

    async def _renew_lease(self, run_id: int, owner: str, fencing_version: int) -> None:
        """周期续租；续租失败即停止继续执行，避免过期执行者写入事实。"""
        renew = getattr(self._turn_reader, "renew_executor_lease", None)
        if not callable(renew):
            return
        try:
            while True:
                await asyncio.sleep(20)
                if renew(run_id, owner, fencing_version) is None:
                    execution = self._executions.get(run_id)
                    if execution is not None:
                        execution.task.cancel()
                    return
        except asyncio.CancelledError:
            raise

    async def _set_status(self, run_id: int, status: ConversationRunStatus) -> None:
        """在锁内更新指定 run 的状态。"""

        async with self._lock:
            execution = self._executions.get(run_id)
            if execution is not None:
                execution.state = ConversationRunState(run_id, status)
