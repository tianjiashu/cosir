"""进程内 ConversationRun 后台执行器。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.assistant_transport.service.conversation_event_projector import ConversationEventProjector
from app.config.logging.logger import log
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.workflows.event import RunStatusChangedEvent, ToolCallsSettledEvent
from app.models import ConversationRunRecord, ConversationRunStatus
from app.service.depends import get_conversation_run_service
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

ConversationRunRunner = Callable[[ConversationRunRecord], Awaitable[None]]


@dataclass(frozen=True)
class ConversationRunState:
    """进程内运行状态的不可变快照。"""

    run_id: int
    status: ConversationRunStatus


@dataclass
class _Execution:
    """执行器内部维护的运行条目。"""

    state: ConversationRunState
    thread_task: asyncio.Task[None]


class ConversationRunExecutor:
    """在进程内独立驱动一次 ConversationRun。"""

    def __init__(
        self,
        run_service: object | None = None,
        cancellation_signal: object | None = None,
        *,
        persist_status: bool = True,
    ) -> None:
        """初始化执行器及其进程内运行注册表。

        参数:
            run_service: 可选 run 状态与查询 service；None 时使用进程级默认实例。
            cancellation_signal: 可选取消信号源；None 时使用进程级取消注册表。
            persist_status: 是否把 run 终态落库并投影到 Transport snapshot；False 时
                执行器只维护进程内状态（供注入了替代 run_service 的场景使用）。

        返回:
            无。

        异常:
            RuntimeError: 若 ``run_service`` 省略且存储尚未初始化。

        副作用:
            无；进程内运行注册表初始为空。
        """

        self._run_service = run_service or get_conversation_run_service()
        self._persist_status = persist_status
        self._event_projector = ConversationEventProjector() if persist_status else None
        self._signal = cancellation_signal or cancellation_registry
        self._executions: dict[int, _Execution] = {}

    async def start(self, run_id: int, runner: ConversationRunRunner) -> asyncio.Task[None]:
        """登记 run 并创建独立后台 task；后台 task 取得 task 锁后才认领 run。

        参数:
            run_id: 运行对应的 ``ConversationRunRecord.id``。
            runner: ``AgentRuntime.execute_run`` 或同签名适配器。

        返回:
            已创建的后台 ``asyncio.Task``。

        异常:
            KeyError: ``run_id`` 对应的 run 不存在。
            ValueError: 该 run 已有未结束的后台执行，或已被其他执行器认领。

        副作用:
            在当前事件循环创建后台 task；HTTP 订阅断开不会取消它。
        """

        # 检查是否有未结束的后台执行
        existing = self._executions.get(run_id)
        if existing is not None and not existing.thread_task.done():
            raise ValueError(f"run {run_id} is already executing")

        run = self._run_service.get_run(run_id)
        state = ConversationRunState(run_id, ConversationRunStatus.PENDING)
        thread_task = asyncio.create_task(self._execute(run_id, run, runner))
        self._executions[run_id] = _Execution(state, thread_task)
        return thread_task

    async def close(self) -> None:
        """优雅关闭执行器并取消活动执行。"""

        executions = list(self._executions.values())
        for execution in executions:
            if not execution.thread_task.done():
                execution.thread_task.cancel()
        if executions:
            await asyncio.gather(
                *(execution.thread_task for execution in executions), return_exceptions=True
            )
        task_runtime_spaces.close()

    async def cancel(self, run_id: int, end_reason: str = "user_cancelled") -> bool:
        """显式取消一个 run：标记运行时信号、仲裁落库、中断后台执行。

        取消时序收敛于本方法（全进程唯一取消编排入口）：
        1. 先在进程内取消信号源标记——工具可能在 ``asyncio.to_thread`` 工作线程
           任意时刻检查信号，信号先行把协作取消的漏检窗口归零；
        2. 再经 run service 做事务性 active → cancelled 状态转移，兼作
           「是否可取消」的仲裁：run 不存在抛 ``KeyError``，已处于终态返回 ``None``；
        3. 最后中断当前进程中的后台执行 task。

        mark 与落库之间无 ``await``，事件循环不会插入其他协程，因此执行器
        ``CancelledError`` 分支的 ``executor_cancelled`` 收尾总会被 ``settle_run``
        的终态守卫吞掉，调用方传入的 ``end_reason``（如 ``user_cancelled``）不会被覆盖。

        参数:
            run_id: 待取消的运行标识。
            end_reason: 写入持久化事实的取消原因，默认 ``user_cancelled``。

        返回:
            取消成功（已落库并尽力中断；未开启状态落库时至少中断本地 task）为
            ``True``；run 已处于不可取消终态为 ``False``。

        异常:
            KeyError: ``run_id`` 对应的 run 不存在——先撤销已标记的信号再向上传播，
                由 API 层映射为 404。

        副作用:
            先写进程内取消信号，再落库取消终态，再向活动 asyncio task 发出取消请求；
            仲裁失败的路径会撤销信号保持一致性；HTTP 订阅断开不会调用本方法。
        """

        self._signal.mark_cancelled(run_id)
        settled: object
        try:
            if not self._persist_status:
                log.warning(
                    "conversation_run_cancel_persistence_disabled",
                    extra={
                        "msg": "执行器未开启状态落库，仅中断本地执行，不落库取消终态",
                        "data": {"run_id": run_id},
                    },
                )
                settled = True
            else:
                settled = self._run_service.cancel_run_if_running(run_id, end_reason)
        except KeyError:
            self._signal.clear(run_id)
            raise
        if settled is None:
            self._signal.clear(run_id)
            return False
        execution = self._executions.get(run_id)
        if execution is None or execution.thread_task.done():
            self._signal.clear(run_id)
            return False
        execution.state = ConversationRunState(run_id, ConversationRunStatus.CANCELLED)
        execution.thread_task.cancel()
        return True

    async def get_status(self, run_id: int) -> ConversationRunState | None:
        """查询一个 run 的当前进程内状态快照。

        参数:
            run_id: 运行标识。

        返回:
            已知 run 的不可变状态快照；未知 run 返回 None。

        异常:
            无。

        副作用:
            无。
        """

        execution = self._executions.get(run_id)
        if execution is not None:
            return execution.state
        try:
            run = self._run_service.get_run(run_id)
        except KeyError:
            return None
        return ConversationRunState(run_id, ConversationRunStatus(run.status))

    async def status(self, run_id: int) -> ConversationRunState | None:
        """查询运行状态；这是 ``get_status`` 的简短别名。

        参数:
            run_id: 运行标识。

        返回:
            与 ``get_status`` 相同的状态快照或 None。

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
        """执行 runner 并收束状态：管理执行注册表生命周期。

        参数:
            run_id: 当前运行标识。
            run: 已认领的运行记录。
            runner: 实际 Agent runtime 执行函数。

        返回:
            无。

        异常:
            ValueError: 该 run 已被其它执行器认领（``claim_pending_run`` 返回 False）。
            ``_run_and_settle`` 的异常继续向上传播（后台 task 内由 asyncio 捕获）。

        副作用:
            经 ``claim_pending_run`` 做同一 Task 内 Run 的认领栅栏（DB 级互斥，无进程内锁）；
            退出时移除执行注册并清理进程内取消信号（清理唯一收口，覆盖取消/失败/完成全部路径）。
        """

        # 同一 Task 的 Run 串行由 DB 级认领保证，进程内不再持锁。
        if not self._run_service.claim_pending_run(run_id):
            raise ValueError(f"run {run_id} was claimed by another executor")
        if self._event_projector is not None:
            self._event_projector.process(
                RunStatusChangedEvent(
                    task_id=run.task_id,
                    run_id=run_id,
                    status=ConversationRunStatus.RUNNING,
                )
            )
        await self._set_status(run_id, ConversationRunStatus.RUNNING)
        try:
            await self._run_and_settle(run_id, run, runner)
        finally:
            current = self._executions.get(run_id)
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if current is not None and current.thread_task is current_task:
                self._executions.pop(run_id, None)
            self._signal.clear(run_id)

    async def _run_and_settle(
        self,
        run_id: int,
        run: ConversationRunRecord,
        runner: ConversationRunRunner,
    ) -> None:
        """执行 runner 并按结果收束持久化终态与进程内状态。

        参数:
            run_id: 当前运行标识。
            run: 已认领的运行记录。
            runner: 实际 Agent runtime 执行函数。

        返回:
            无。

        异常:
            runner 的异常被记录并转换为 failed；取消异常按 cancelled 收束后继续抛出。

        副作用:
            经 run service 写入 completed / cancelled / failed 终态与未决工具收口，
            并更新执行器内该 run 的不可变状态快照。
        """

        try:
            await runner(run)
            if self._persist_status:
                self._run_service.complete_run_if_running(run_id)
            await self._set_status(run_id, ConversationRunStatus.COMPLETED)
        except asyncio.CancelledError:
            if self._persist_status:
                self._project_tools_settled(run_id, "cancelled", "executor_cancelled")
                self._run_service.cancel_run_if_running(run_id, end_reason="executor_cancelled")
            await self._set_status(run_id, ConversationRunStatus.CANCELLED)
            raise
        except Exception:
            if self._persist_status:
                self._project_tools_settled(run_id, "failed", "runtime_failed")
                self._run_service.fail_run_if_running(run_id, end_reason="runtime_failed")
            await self._set_status(run_id, ConversationRunStatus.FAILED)
            log.exception(
                "conversation_run_failed",
                extra={"msg": "后台 ConversationRun 执行失败", "data": {"run_id": run_id}},
            )

    def _project_tools_settled(self, run_id: int, status: str, reason: str) -> None:
        """通过统一 event projector 收束执行器遗留的工具调用。"""

        if self._event_projector is None:
            return
        run = self._run_service.get_run(run_id)
        self._event_projector.process(
            ToolCallsSettledEvent(
                task_id=run.task_id,
                run_id=run_id,
                status="cancelled" if status == "cancelled" else "failed",
                reason=reason,
            )
        )

    async def _set_status(self, run_id: int, status: ConversationRunStatus) -> None:
        """更新指定 run 的进程内状态快照。

        参数:
            run_id: 运行标识。
            status: 新的进程内状态。

        返回:
            无。

        异常:
            无。

        副作用:
            更新执行器内对应条目的不可变状态快照。
        """

        execution = self._executions.get(run_id)
        if execution is not None:
            execution.state = ConversationRunState(run_id, status)
