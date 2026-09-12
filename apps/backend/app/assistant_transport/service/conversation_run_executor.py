"""进程内 ConversationRun 后台执行器。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from app.assistant_transport.event import ToolCallsSettledEvent
from app.assistant_transport.service.conversation_event_projector import ConversationEventProjector
from app.config.logging.logger import log
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.models import ConversationRunRecord, ConversationRunStatus
from app.service.depends import get_conversation_run_service
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

ConversationRunRunner = Callable[[ConversationRunRecord], Awaitable[None]]
CANCEL_WAIT_TIMEOUT_SECONDS = 5.0


class _RunService(Protocol):
    """Conversation Run service operations required by the executor."""

    def get_run(self, run_id: int) -> ConversationRunRecord: ...

    def claim_pending_run(self, run_id: int) -> bool: ...

    def claim_or_resume_run(self, run_id: int) -> bool: ...

    def complete_run_if_running(self, run_id: int) -> ConversationRunRecord | None: ...

    def cancel_run_if_running(
        self,
        run_id: int,
        end_reason: str = "user_cancelled",
        final_output: str | None = None,
    ) -> ConversationRunRecord | None: ...

    def fail_run_if_running(
        self,
        run_id: int,
        end_reason: str | None = None,
        final_output: str | None = None,
    ) -> ConversationRunRecord | None: ...


class _CancellationSignal(Protocol):
    """Process-local cancellation signal operations required by the executor."""

    def mark_cancelled(self, run_id: int) -> None: ...

    def clear(self, run_id: int) -> None: ...


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
        run_service: _RunService | None = None,
        cancellation_signal: _CancellationSignal | None = None,
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

        self._run_service: _RunService = run_service or get_conversation_run_service()
        self._persist_status = persist_status
        self._event_projector = ConversationEventProjector() if persist_status else None
        self._signal: _CancellationSignal = cancellation_signal or cancellation_registry
        self._executions: dict[int, _Execution] = {}
        self._cancelling_run_ids: set[int] = set()
        self._cancellation_cleanup_tasks: set[asyncio.Task[None]] = set()

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
        if run_id in self._cancelling_run_ids:
            raise ValueError(f"run {run_id} cancellation is still in progress")
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

    def mark_task_deleted(self, task_id: int) -> None:
        """阻止该执行器实例继续投影已删除 Task 的迟到事件。"""

        if self._event_projector is not None:
            self._event_projector.mark_task_deleted(task_id)

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
            若线程工具超过取消等待窗口，run 会保持 ``cancelling`` 门闩，直到旧 task
            真正结束后才允许 resume，避免以“快速返回”为代价造成两个执行器重叠。仲裁
            失败的路径会撤销信号保持一致性；HTTP 订阅断开不会调用本方法。
        """

        # 这是同步集合操作，且在第一个 await 之前完成。恢复入口可以据此把
        # “取消已开始但数据库尚未切换”的短窗口视为冲突，避免旧 task 与新
        # resume task 交叉运行同一个 task 的 context / tool / snapshot。
        if run_id in self._cancelling_run_ids:
            return False
        self._cancelling_run_ids.add(run_id)
        self._signal.mark_cancelled(run_id)
        try:
            self._run_service.get_run(run_id)
        except KeyError:
            self._cancelling_run_ids.discard(run_id)
            self._signal.clear(run_id)
            raise
        execution = self._executions.get(run_id)
        execution_was_active = execution is not None and not execution.thread_task.done()
        try:
            # 先以数据库状态作为取消闸门，再取消 asyncio task。这样 task 的
            # CancelledError 收口看到的必然是 user_cancelled，不会抢先写成
            # executor_cancelled；resume 也只能在取消请求完成后重开同一个 run。
            if not self._persist_status:
                log.warning(
                    "conversation_run_cancel_persistence_disabled",
                    extra={
                        "msg": "执行器未开启状态落库，仅中断本地执行，不落库取消终态",
                        "data": {"run_id": run_id},
                    },
                )
                settled: object = True
            else:
                settled = self._run_service.cancel_run_if_running(run_id, end_reason)
                if settled is not None:
                    # 取消是终止当前工具调用的事实，不再把 user_cancelled 当作“可重放
                    # 工具”的特殊分支。事件投影先收口 UI；运行上下文在旧 task 收束后
                    # 补 ToolMessage，供 resume 从当前上下文继续。
                    self._project_tools_settled(run_id, "cancelled", end_reason)

            if execution_was_active:
                execution.state = ConversationRunState(run_id, ConversationRunStatus.CANCELLED)
                execution.thread_task.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(execution.thread_task),
                        timeout=CANCEL_WAIT_TIMEOUT_SECONDS,
                    )
                except asyncio.CancelledError:
                    # 旧执行 task 正常响应取消后会以 CancelledError 结束；该异常属于
                    # 被取消的子 task，不应冒泡成取消 API 自身失败。
                    if not execution.thread_task.done():
                        # 如果子 task 还没结束，CancelledError 来自 cancel() 自身，不能
                        # 把调用方取消误吞掉；后台 task 由 finally 中的 cleanup 继续收束。
                        raise
                except TimeoutError:
                    log.warning(
                        "conversation_run_cancel_wait_timeout",
                        extra={
                            "msg": "取消等待旧 run 收束超时，暂不允许同 run resume",
                            "data": {
                                "run_id": run_id,
                                "timeout_seconds": CANCEL_WAIT_TIMEOUT_SECONDS,
                            },
                        },
                    )
        except KeyError:
            raise
        finally:
            if execution is None or execution.thread_task.done():
                self._signal.clear(run_id)
                self._cancelling_run_ids.discard(run_id)
            else:
                # 旧 task 仍可能在 worker 线程里检查取消信号；让它保持到
                # ``_execute`` 的 finally，避免超时返回后过早清除协作取消标记。
                cleanup_task = asyncio.create_task(
                    self._release_cancellation_after(run_id, execution.thread_task)
                )
                self._cancellation_cleanup_tasks.add(cleanup_task)
                cleanup_task.add_done_callback(self._cancellation_cleanup_tasks.discard)
        if settled is None:
            return execution_was_active
        await self._set_status(run_id, ConversationRunStatus.CANCELLED)
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

    def is_locally_running(self, run_id: int) -> bool:
        """返回当前进程是否仍持有指定 run 的后台执行 task。"""

        execution = self._executions.get(run_id)
        return execution is not None and not execution.thread_task.done()

    def is_cancelling(self, run_id: int) -> bool:
        """返回指定 run 是否正在执行取消编排。"""

        return run_id in self._cancelling_run_ids

    async def _release_cancellation_after(
        self, run_id: int, execution_task: asyncio.Task[None]
    ) -> None:
        """等待超时的旧执行结束，再释放同 run 的恢复门闩。"""

        try:
            await execution_task
        except BaseException as error:
            # 旧 task 的异常已经由 ``_run_and_settle`` 或 asyncio task 生命周期
            # 收口；这里仅负责释放并发门闩，不重复记录同一异常。
            log.debug(
                "conversation_run_cancel_cleanup_finished_with_error",
                extra={
                    "msg": "取消收束监视任务观察到旧执行异常",
                    "data": {"run_id": run_id, "error_type": type(error).__name__},
                },
            )
        finally:
            self._cancelling_run_ids.discard(run_id)

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

        try:
            task_space = task_runtime_spaces.get_or_create(run.task_id)
        except KeyError:
            log.info(
                "conversation_run_skipped_for_deleted_task",
                extra={
                    "msg": "runtime space 已卸载，跳过已删除 task 的 Conversation Run",
                    "data": {"task_id": run.task_id, "run_id": run_id},
                },
            )
            return
        try:
            # DB claim 只保证状态转移幂等；Task 操作闸门才保证同一 Task 的 context、
            # sequence、tool 资源和 snapshot 投影在整个执行生命周期内串行。
            async with task_space.async_operation():
                # start() 与真正调度 _execute 之间存在一次 event-loop 让出窗口；
                # task 可能已经在该窗口被删除，因此必须在取得 Task 闸门后重新读取
                # run，不能继续信任调度前传入的对象。
                try:
                    current_run = self._run_service.get_run(run_id)
                except KeyError:
                    log.info(
                        "conversation_run_skipped_after_task_delete",
                        extra={
                            "msg": "task 删除后跳过已排队的 Conversation Run",
                            "data": {"task_id": run.task_id, "run_id": run_id},
                        },
                    )
                    task_runtime_spaces.unload(run.task_id, expected_space=task_space)
                    return
                if current_run.task_id != run.task_id:
                    log.warning(
                        "conversation_run_task_mismatch",
                        extra={
                            "msg": "Conversation Run 所属 task 在执行前发生变化，跳过执行",
                            "data": {
                                "task_id": run.task_id,
                                "run_id": run_id,
                                "actual_task_id": current_run.task_id,
                            },
                        },
                    )
                    return
                run = current_run
                if not self._run_service.claim_or_resume_run(run_id):
                    raise ValueError(f"run {run_id} was claimed by another executor")
                await self._set_status(run_id, ConversationRunStatus.RUNNING)
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
                current = self._run_service.get_run(run_id)
                if not (
                    current.status == ConversationRunStatus.CANCELLED.value
                    and current.end_reason == "user_cancelled"
                ):
                    self._run_service.cancel_run_if_running(
                        run_id, end_reason="executor_cancelled"
                    )
                    self._project_tools_settled(run_id, "cancelled", "executor_cancelled")
            await self._set_status(run_id, ConversationRunStatus.CANCELLED)
            raise
        except Exception:
            if self._persist_status:
                self._run_service.fail_run_if_running(run_id, end_reason="runtime_failed")
                self._project_tools_settled(run_id, "failed", "runtime_failed")
            await self._set_status(run_id, ConversationRunStatus.FAILED)
            log.exception(
                "conversation_run_failed",
                extra={"msg": "后台 ConversationRun 执行失败", "data": {"run_id": run_id}},
            )

    def _project_tools_settled(self, run_id: int, status: str, reason: str) -> None:
        """通过统一 event projector 收束执行器遗留的工具调用。

        这是 canonical Run 终态提交后的 Transport 旁路；任何读取或投影失败都只记录，
        不得把已收束的 workflow 再次打回 active/failed 异常路径。
        """

        try:
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
        except Exception:
            log.exception(
                "conversation_run_tool_settlement_projector_failed",
                extra={
                    "msg": "Run 终态已提交，工具收束 projector 失败并被降级",
                    "data": {"run_id": run_id, "status": status, "reason": reason},
                },
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
