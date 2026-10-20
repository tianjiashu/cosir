"""进程内 ConversationRun 后台执行器。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.assistant_transport.event import ToolCallsSettledEvent
from app.assistant_transport.service.conversation_event_projector import ConversationEventProjector
from app.config.logging.logger import log
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.execution_mode import ExecutionMode
from app.core.runtime.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.models import ConversationRunRecord, ConversationRunStatus
from app.service.depends import (
    get_conversation_run_service,
    get_conversation_run_state_service,
    get_runtime,
    get_task_service,
)

ConversationRunRunner = Callable[[ConversationRunRecord], Awaitable[None]]


@dataclass
class _Execution:
    """执行器内部维护的运行条目。

    ``run_id`` 不在此重复存放：它已经是 ``_executions`` 字典的键。
    """

    task_id: int
    thread_task: asyncio.Task[None]


class ConversationRunExecutor:
    """在进程内独立驱动一次 ConversationRun，并作为进程内取消信号的标记入口。

    本执行器只做「执行」：不拥有 run 的业务终态（``running`` → ``completed`` / ``failed`` /
    ``cancelled`` 由 workflow 落定），也不决定业务准入（由 ``prepare_run_start`` 判定）。
    唯一例外是兜底收敛：驱动结束后 run 仍 ``pending`` / ``running``（workflow 进入前抛错、
    落终态写库失败、关闭期取消）时，``_execute`` 以条件更新把它收敛为终态，保证不遗留
    无驱动者的 active run；条件更新使其对 workflow 已落定的 run 是 no-op，终态唯一写入
    者仍是 workflow。

    取消入口同属本类：``cancel`` 只向 Task 的当前执行发信号（等待态以强制拒绝恢复后同样交
    循环收敛），``cancel_tool_call`` 标记工具级取消（只中止一次工具调用，run 继续）。
    执行释放是 Task 信号的唯一清理点。Run 业务终态仍由 workflow 落定；信号不跨进程、不跨重启。
    """

    def __init__(
        self,
    ) -> None:
        """装配 run/state service、事件投影器、进程内取消信号源，并初始化空运行注册表。"""

        self._run_service = get_conversation_run_service()
        self._run_state_service = get_conversation_run_state_service()
        self._task_service = get_task_service()
        self._event_projector = ConversationEventProjector()
        self._signal = cancellation_registry
        self._executions: dict[int, _Execution] = {}
        self._closing = False

    def has_execution(self, run_id: int) -> bool:
        return run_id in self._executions

    def _task_execution(self, task_id: int) -> int | None:
        """返回该 Task 当前执行的 run_id；同一 Task 同时至多一个执行者。"""
        return next(
            (run_id for run_id, item in self._executions.items() if item.task_id == task_id),
            None,
        )

    async def wait_released(self, run_id: int) -> None:
        """恢复/编辑在 Task 闸门外等待旧驱动，防止持锁等待收尾造成死锁。"""
        execution = self._executions.get(run_id)
        if execution is not None and execution.thread_task is not asyncio.current_task():
            await asyncio.shield(execution.thread_task)

    async def start(
        self,
        run_id: int,
        start_mode: ExecutionMode,
    ) -> asyncio.Task[None]:
        """登记 run 并创建独立后台 task。

        仅做「canonical 前置断言 + 进程内登记 + 建 task」：确认目标 run 已处于 ``running``
        （由 ``prepare_run_start`` 经 ``claim_pending_run`` 落定）否则抛 ``ValueError``
        视作调用方违约；不做业务准入、不落库、不认领 run、不等待结果；HTTP 订阅断开不会取消该
        task。``start_mode`` 原样透传；等待决定只从持久化 Run 中读取。
        """

        if run_id in self._executions:
            raise ValueError("Run 已有执行者")
        run = await asyncio.to_thread(self._run_service.get_run, run_id)
        if run.status != ConversationRunStatus.RUNNING:
            raise ValueError(f"run {run_id} is not running")
        thread_task = asyncio.create_task(self._execute(run_id, start_mode))
        # 后台 task 无人 await：不挂回调时其异常会被 asyncio 静默吞掉，排障只能靠间接日志。
        thread_task.add_done_callback(lambda task: self._log_execution_result(run_id, task))
        self._executions[run_id] = _Execution(task_id=run.task_id, thread_task=thread_task)
        return thread_task

    @staticmethod
    def _log_execution_result(run_id: int, task: asyncio.Task[None]) -> None:
        """后台 task 异常结束兜底记录，确保「task 异常收尾」永不静默。

        正常路径下 ``_execute`` 已在内部收口驱动期异常，task 不会以异常结束；此回调一旦触发，
        说明有异常绕过了收口，须显式记录。task 正常/取消结束直接返回，inspection 自身失败静默忽略。
        """

        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.exception(
                "conversation_run_execution_done_callback_failed",
                extra={
                    "msg": "Run done callback 读取异常失败",
                    "data": {"run_id": run_id, "error_type": type(exc).__name__},
                },
            )
            return
        if error is None:
            return
        log.error(
            "conversation_run_execution_failed",
            extra={
                "msg": "后台 Run 执行 task 以异常结束（终态由 workflow 落定）",
                "data": {
                    "run_id": run_id,
                    "error_type": type(error).__name__,
                    "error": str(error)[:500],
                },
            },
        )

    async def close(self) -> None:
        """优雅关闭：取消所有未完成后台 task 并 await 收尾，随后清空进程内执行登记。

        用于应用生命周期关闭（调用方处于事件循环内、可 await）；单个 task 收尾异常经
        ``gather(return_exceptions=True)`` 吞掉。registry 清理与 :meth:`close_sync` 对齐。
        """

        self._closing = True
        executions = list(self._executions.values())
        # 先发 Task 信号：线程/子进程工具只认信号，asyncio 取消到不了工具线程，进程树会拖到超时。
        for execution in executions:
            self._signal.request_cancel(execution.task_id)
        for execution in executions:
            if not execution.thread_task.done():
                execution.thread_task.cancel()
        if executions:
            await asyncio.gather(
                *(execution.thread_task for execution in executions), return_exceptions=True
            )
        # 各执行的 finally 已经释放并取走信号；这里只清理未登记完成的残留条目。
        self._executions.clear()

    def close_sync(self) -> None:
        """同步围栏运行时执行，供不能 await 的测试/依赖重配置路径使用。

        跨线程安全：对每个未完成 task 经其所属 loop 的 ``call_soon_threadsafe`` 投递取消，
        再清空进程内索引；正常 lifespan 走可 await 的 :meth:`close`。
        """

        self._closing = True
        executions = tuple(self._executions.items())
        for run_id, execution in executions:
            # 不等待收尾，释放边界不一定能运行：在此取走信号，避免残留到下一次装配。
            self._signal.consume(execution.task_id)
            if execution.thread_task.done():
                continue
            try:
                execution.thread_task.get_loop().call_soon_threadsafe(execution.thread_task.cancel)
            except RuntimeError:
                log.warning(
                    "conversation_run_executor_sync_close_cancel_failed",
                    extra={
                        "msg": "同步 reset 无法向 Run 所属事件循环投递取消",
                        "data": {"run_id": run_id},
                    },
                )
        self._executions.clear()

    async def cancel(self, task_id: int) -> bool:
        """向 Task 的当前执行发送取消信号；只发信号，终态由 workflow 在检查点落定。

        - 有活动执行：同步登记信号即返回；检查与登记之间没有 await，与释放边界的「注销 +
          取走信号」在同一事件循环上互斥，信号不会落在已释放的执行上。
        - 无执行且最新 Run 在等待用户：在 Task 闸门内登记信号，并以 ``FORCE_REJECT`` 恢复
          等待——循环按拒绝结算待决调用，到 MODEL 检查点落定 cancelled。与提交决定争用同一次
          ``waiting → running`` 条件迁移，后到者得到「不可恢复」。
        - 无执行但最新 Run 为 running（Agent Team 节点由 Coordinator 驱动）：登记信号，由
          Coordinator 在节点 Run 结束后取走。
        - 其余（终态或无 Run）：不登记，返回 False。

        向 Team、子 Agent 的传播在本 Run 落定 cancelled 后由释放边界发出，见
        :meth:`_propagate_cancel`。

        异常:
            KeyError: task 不存在。
        """
        if self._task_execution(task_id) is not None:
            self._signal.request_cancel(task_id)
            return True
        await asyncio.to_thread(self._task_service.get_task, task_id)
        latest = await asyncio.to_thread(self._task_service.get_latest_run, task_id)
        if latest is None:
            return False
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        async with task_runtime_spaces.get_or_create(task_id).async_operation(wait_seconds=10):
            # 闸门内重查：等待期间可能已有提交决定或交接启动了执行。
            if self._task_execution(task_id) is not None:
                self._signal.request_cancel(task_id)
                return True
            run = await asyncio.to_thread(self._run_service.get_run, latest.id)
            if run.status == ConversationRunStatus.WAITING_FOR_INPUT:
                return await self._resume_waiting_for_cancel(run)
            if run.status == ConversationRunStatus.RUNNING:
                # 不经本执行器驱动的 running Run（Agent Team 节点由 Coordinator 直接驱动）：
                # 同样只发信号，由驱动方在节点 Run 结束后取走。
                self._signal.request_cancel(task_id)
                return True
            return False

    async def stop_tasks(self, task_ids: set[int]) -> None:
        """删除前停止这些 Task 的执行并等待释放；只发信号，终态仍由各自循环落定。

        必须在删除闸门之外调用：执行收尾需要取得同一 Task 闸门，持锁等待会自锁。
        """
        for task_id in task_ids:
            await self.cancel(task_id)
        for task_id in task_ids:
            # 等待态取消会在 cancel 内同步登记新的执行，这里一并等到它落定。
            run_id = self._task_execution(task_id)
            if run_id is not None:
                await self.wait_released(run_id)

    async def _resume_waiting_for_cancel(self, run: ConversationRunRecord) -> bool:
        """调用方须持有 Task 闸门且该 Task 无执行：登记信号后以强制拒绝恢复等待 Run。"""
        from app.core.tools.schemas import UserDecision, UserDecisionKind
        from app.core.workflow.node_helper.user_input_projection import extract_request

        continuation = run.execution_data.continuation
        request = extract_request(continuation.observations) if continuation else None
        if request is None:
            log.error(
                "waiting_run_cancel_missing_request",
                extra={
                    "msg": "等待态 Run 缺少待决请求，无法以强制拒绝恢复",
                    "data": {"run_id": run.id, "task_id": run.task_id},
                },
            )
            return False
        self._signal.request_cancel(run.task_id)
        resumed = await asyncio.to_thread(
            self._run_state_service.resume_waiting_run,
            run.id,
            user_decision=UserDecision(
                request_id=request.request_id, kind=UserDecisionKind.FORCE_REJECT
            ),
        )
        if resumed is None:
            self._signal.consume(run.task_id)
            return False
        try:
            await self.start(run.id, "resume_with_input")
        except BaseException:
            # 已迁移为 running 却没有执行者：兜底收敛为 cancelled，信号随之作废。
            self._signal.consume(run.task_id)
            await self._converge_unfinished_run(run.id)
            raise
        log.info(
            "waiting_run_cancel_resumed",
            extra={
                "msg": "等待态 Run 以强制拒绝恢复，交由循环落定取消",
                "data": {"run_id": run.id, "task_id": run.task_id},
            },
        )
        return True

    async def cancel_tool_call(self, run_id: int, tool_call_id: str) -> bool:
        """标记工具级取消信号（只中止一次工具调用，run 继续）。

        只在 ``tool_call_cancellation_registry`` 标记 ``(run_id, tool_call_id)``，不落库、
        不取消 run 或后台 task；工具执行层检出后返回 ``cancelled`` 观察，run 终态仍由 workflow
        落定。存在性预检同 :meth:`cancel`。

        返回: 新标记返回 ``True``；已标记则返回 ``False``（已结束调用被重新标记会再返 ``True``，
        因其信号随单次调用结束释放、run 收尾时按 run_id 兜底清理）。
        """

        # 标记是同步集合操作且在第一个 await 之前完成：本调用返回后，该工具调用的取消
        # 信号即可被工具执行层通过轮询观测到。本方法不落库，因此数据库中的 run 状态与
        # 工具调用状态都不会因这次调用而改变。
        if tool_call_cancellation_registry.is_cancelled(run_id, tool_call_id):
            return False
        tool_call_cancellation_registry.mark_cancelled(run_id, tool_call_id)
        try:
            await asyncio.to_thread(self._run_service.get_run, run_id)
        except KeyError:
            tool_call_cancellation_registry.clear(run_id, tool_call_id)
            raise
        return True

    async def _execute(
        self,
        run_id: int,
        start_mode: ExecutionMode,
    ) -> None:
        """驱动一次 run 执行；执行器不拥有 run 终态（由 workflow 落定），只负责收尾兜底。

        读取 run → 解析本次 run 独占的 agent profile → 交给 ``run_agent`` 驱动 workflow；
        退出时先兜底收敛 Run 状态，再调用 workflow 的终态收尾，最后在释放边界注销登记并取走 Task
        取消信号（见 :meth:`_release_and_handoff`）。

        驱动期 ``Exception`` **不外抛**，由 workflow 或 ``_converge_unfinished_run`` 兜底；
        ``CancelledError`` 照常上浮，收尾仍在 finally 中执行。
        """
        runner_started = False
        runtime = None
        agent_profile = None
        try:
            # canonical 读取一律走线程池：事件循环线程上不做数据库 IO（与 start / cancel 同口径）。
            run = await asyncio.to_thread(self._run_service.get_run, run_id)
            runner_started = True
            runtime = get_runtime()
            agent_profile = runtime.resolve_agent_profile_for_run(run)
            await runtime.run_agent(
                agent_profile,
                execution_mode=start_mode,
            )
        except Exception:
            log.exception(
                "conversation_run_execution_failed",
                extra={
                    "msg": "Run 驱动过程发生未收敛异常，继续执行统一收尾",
                    "data": {"run_id": run_id, "runner_started": runner_started},
                },
            )
        finally:
            # 先收敛 canonical Run 状态，再执行工作流自身的收尾；Task 终端不随 Run 清理。
            await self._converge_unfinished_run(run_id)
            if runtime is not None and agent_profile is not None:
                try:
                    await runtime.finalize_run(agent_profile, run_id)
                except Exception:
                    log.exception(
                        "conversation_run_workflow_finalize_failed",
                        extra={
                            "msg": "工作流资源收尾失败，Run 状态已独立收敛",
                            "data": {"run_id": run_id},
                        },
                    )
            await self._release_and_handoff(run_id)

    async def _release_and_handoff(self, run_id: int) -> None:
        """释放驱动并按释放瞬间的取消信号决定后续驱动。

        释放（注销执行 + 取走 Task 信号）是信号的唯一清理点，因此这里还要接住「最后一个检查点
        之后才到达」的取消：
        - 挂起为等待且有取消：以强制拒绝恢复，交循环落定 cancelled；
        - 正常完成且有取消：用户要求停止，不为 inbox 接续新 Run；
        - 正常完成且无取消：在 Task 闸门内为剩余 inbox 交接新 Run；
        - 落定 cancelled：向 Agent Team 与子 Agent 发出取消。
        """
        from app.service.depends import get_conversation_run_command_service
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        try:
            try:
                run = await asyncio.to_thread(self._run_service.get_run, run_id)
            except KeyError:
                # 终态已落库后允许删除历史；删除无需等待网络 client 的最终关闭。
                log.info(
                    "run_released_after_deletion",
                    extra={"msg": "已删除 Run 的执行者完成释放", "data": {"run_id": run_id}},
                )
                return
            if self._closing:
                return
            if run.status == ConversationRunStatus.WAITING_FOR_INPUT:
                if not self._release(run_id):
                    return
                async with task_runtime_spaces.get_or_create(run.task_id).async_operation(
                    wait_seconds=10
                ):
                    # 释放后到这里之间，新的取消请求会自行走等待态取消；只在仍无执行时接手。
                    run = await asyncio.to_thread(self._run_service.get_run, run_id)
                    if (
                        self._task_execution(run.task_id) is None
                        and run.status == ConversationRunStatus.WAITING_FOR_INPUT
                    ):
                        await self._resume_waiting_for_cancel(run)
                return
            if run.status != ConversationRunStatus.COMPLETED:
                self._release(run_id)
                if run.status == ConversationRunStatus.CANCELLED:
                    await self._propagate_cancel(run)
                return
            async with task_runtime_spaces.get_or_create(run.task_id).async_operation(
                wait_seconds=10
            ):
                if self._release(run_id):
                    log.info(
                        "run_handoff_skipped_by_cancel",
                        extra={
                            "msg": "Run 完成时已收到停止请求，不为排队输入接续新 Run",
                            "data": {"run_id": run_id, "task_id": run.task_id},
                        },
                    )
                    return
                next_run = await asyncio.to_thread(
                    get_conversation_run_command_service().start_queued_run, run.task_id
                )
                if next_run is not None:
                    try:
                        await self.start(next_run.run.id, "fresh")
                    except BaseException:
                        await self._converge_unfinished_run(next_run.run.id)
                        raise
        finally:
            self._release(run_id)

    def _release(self, run_id: int) -> bool:
        """注销本执行并取走其 Task 信号，返回释放前是否收到取消。

        只注销当前 asyncio task 登记的执行：等待态取消会以同一 run_id 登记新执行，旧执行的
        兜底释放不得误删它或取走它的信号。注销与取走之间无 await，与 :meth:`cancel` 的
        「查执行 + 登记」在事件循环上互斥。
        """
        execution = self._executions.get(run_id)
        if execution is None or execution.thread_task is not asyncio.current_task():
            return False
        del self._executions[run_id]
        return self._signal.consume(execution.task_id)

    async def _propagate_cancel(self, run: ConversationRunRecord) -> None:
        """Run 落定 cancelled 后向下游发出取消；传播失败只记录，不影响本 Run 终态。"""
        from app.agent_team.coordinator import get_agent_team_coordinator

        try:
            await asyncio.to_thread(get_agent_team_coordinator().cancel_for_parent_run_id, run.id)
        except Exception:
            log.exception(
                "agent_team_parent_cancel_propagation_failed",
                extra={"msg": "主 Agent 取消后传播到 Agent Team 失败", "data": {"run_id": run.id}},
            )
        children = await asyncio.to_thread(self._task_service.list_child_tasks, run.task_id)
        for child in children:
            try:
                await self.cancel(child.id)
            except Exception:
                log.exception(
                    "child_agent_cancel_propagation_failed",
                    extra={
                        "msg": "主 Agent 取消后传播到子 Agent 失败",
                        "data": {"run_id": run.id, "child_task_id": child.id},
                    },
                )

    async def _converge_unfinished_run(
        self,
        run_id: int,
    ) -> None:
        """驱动结束后 run 仍未落终态时的兜底收敛安全网：条件更新为 ``cancelled``。

        兜住「run 被 ``prepare_run_start`` 翻成 running 后驱动死亡」的路径（workflow 进入前
        setup 抛错、落终态写库失败、关闭期取消）。正常终态由 workflow 落定，对已落终态的 run
        是 no-op（条件更新保证终态业务写入者仍是 workflow）。

        任何读写失败只记日志、不抛出，避免打断 ``_execute`` 收尾；残余僵尸由下次启动
        ``recover_orphaned_runs`` 兜底。
        """

        try:
            await asyncio.to_thread(
                self._run_state_service.cancel_run_if_running,
                run_id,
                "run_execution_cancelled",
            )
            if self._event_projector is None:
                return
            run = await asyncio.to_thread(self._run_service.get_run, run_id)
            if run.status not in {"cancelled", "failed"}:
                return
            self._event_projector.process(
                ToolCallsSettledEvent(
                    task_id=run.task_id,
                    run_id=run_id,
                    status="cancelled",
                    reason="runtime_cancelled",
                )
            )
        except Exception:
            log.exception(
                "conversation_run_executor_converge_unfinished_failed",
                extra={
                    "msg": "兜底收敛未落终态的 run 失败，依赖下次启动 recover 兜底",
                    "data": {"run_id": run_id},
                },
            )
            return
