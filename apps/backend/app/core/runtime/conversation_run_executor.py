"""进程内 ConversationRun 后台执行器。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from app.assistant_transport.event import ToolCallsSettledEvent
from app.assistant_transport.service.conversation_event_projector import ConversationEventProjector
from app.config.logging.logger import log
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.execution_mode import ExecutionMode
from app.core.runtime.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.tools.schemas.user_decision import UserDecision
from app.models import ConversationRunRecord, ConversationRunStatus
from app.service.depends import (
    get_conversation_run_service,
    get_conversation_run_state_service,
    get_runtime,
    get_terminal_session_service,
)

ConversationRunRunner = Callable[[ConversationRunRecord], Awaitable[None]]


@dataclass
class _Execution:
    """执行器内部维护的运行条目。

    ``run_id`` 不在此重复存放：它已经是 ``_executions`` 字典的键。
    """

    thread_task: asyncio.Task[None]


class ConversationRunExecutor:
    """在进程内独立驱动一次 ConversationRun，并作为进程内取消信号的标记入口。

    本执行器只做「执行」：不拥有 run 的业务终态（``running`` → ``completed`` / ``failed`` /
    ``cancelled`` 由 workflow 落定），也不决定业务准入（由 ``prepare_run_start`` 判定）。
    唯一例外是兜底收敛：驱动结束后 run 仍 ``pending`` / ``running``（workflow 进入前抛错、
    落终态写库失败、关闭期取消）时，``_execute`` 以条件更新把它收敛为终态，保证不遗留
    无驱动者的 active run；条件更新使其对 workflow 已落定的 run 是 no-op，终态唯一写入
    者仍是 workflow。

    取消入口同属本类：``cancel`` 标记 run 级取消并立即强制关闭该 Run 的 terminal（整个
    run 停止），``cancel_tool_call`` 标记工具级取消（只中止一次工具调用，run 继续）。
    Run 业务终态仍由 workflow 落定；信号本身不跨进程、不跨重启。
    """

    def __init__(
            self,
    ) -> None:
        """装配 run/state service、事件投影器、进程内取消信号源，并初始化空运行注册表。"""

        self._run_service = get_conversation_run_service()
        self._run_state_service = get_conversation_run_state_service()
        self._event_projector = ConversationEventProjector()
        self._signal = cancellation_registry
        self._executions: dict[int, _Execution] = {}
        self._terminal_session_service = get_terminal_session_service()

    async def start(
            self,
            run_id: int,
            start_mode: ExecutionMode,
            user_decisions: Sequence[UserDecision] = (),
    ) -> asyncio.Task[None]:
        """登记 run 并创建独立后台 task。

        仅做「canonical 前置断言 + 进程内登记 + 建 task」：确认目标 run 已处于 ``running``
        （由 ``prepare_run_start`` 经 ``claim_pending_run`` 落定）否则抛 ``ValueError``
        视作调用方违约；不做业务准入、不落库、不认领 run、不等待结果；HTTP 订阅断开不会取消该
        task。``start_mode``/``user_decisions`` 原样透传至 ``run_agent``（续跑决定经 workflow
        的 ``Command(resume=...)``）。
        """

        run = await asyncio.to_thread(self._run_service.get_run, run_id)
        if run.status != ConversationRunStatus.RUNNING:
            raise ValueError(f"run {run_id} is not running")
        thread_task = asyncio.create_task(self._execute(run_id, start_mode, user_decisions))
        # 后台 task 无人 await：不挂回调时其异常会被 asyncio 静默吞掉，排障只能靠间接日志。
        thread_task.add_done_callback(lambda task: self._log_execution_result(run_id, task))
        self._executions[run_id] = _Execution(thread_task=thread_task)
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

        executions = list(self._executions.values())
        for execution in executions:
            if not execution.thread_task.done():
                execution.thread_task.cancel()
        if executions:
            await asyncio.gather(
                *(execution.thread_task for execution in executions), return_exceptions=True
            )
        # 与 close_sync 对齐：await 收尾后清空进程内执行登记，避免残留已结束 task 条目。
        self._executions.clear()

    def close_sync(self) -> None:
        """同步围栏运行时执行，供不能 await 的测试/依赖重配置路径使用。

        跨线程安全：对每个未完成 task 经其所属 loop 的 ``call_soon_threadsafe`` 投递取消，
        再清空进程内索引；正常 lifespan 走可 await 的 :meth:`close`。
        """

        executions = tuple(self._executions.items())
        for run_id, execution in executions:
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

    async def cancel(self, run_id: int, end_reason: str = "user_cancelled") -> bool:
        """标记 run 级取消信号并立即关闭其 terminal（整个 run 停止）。

        只发进程内信号、不落库、不直接取消 asyncio task——runner 观测到信号后自行收束，
        终态仍由 workflow 落定。存在性预检：标记后才确认 run 存在，不存在则回滚信号再抛
        ``KeyError``（API 层映射 404）。主 Agent 取消会传播到其 Agent Team（team 尚未创建的
        内部暂停语义不反向取消）。

        返回: 新标记取消请求返回 ``True``；已标记则返回 ``False``。
        """

        # 标记是同步集合操作且在第一个 await 之前完成：本调用返回后，该 run 的取消
        # 信号即可被工具与 runner 通过轮询观测到。本方法不落库，因此数据库中的 run
        # 状态不会因这次调用而改变。
        if cancellation_registry.is_cancelled(run_id):
            return False
        self._signal.mark_cancelled(run_id)
        try:
            await asyncio.to_thread(self._run_service.get_run, run_id)
        except KeyError:
            self._signal.clear(run_id)
            raise
        # 主 Agent 被用户或其它业务入口显式取消时，不能留下仍在运行的 Team。等待用户
        # 确认是 Agent Team 自己使用的内部暂停语义，此时 Team 尚未创建，不能反向取消。
        try:
            from app.agent_team.coordinator import get_agent_team_coordinator

            await asyncio.to_thread(
                get_agent_team_coordinator().cancel_for_parent_run_id,
                run_id,
            )
        except Exception:
            log.exception(
                "agent_team_parent_cancel_propagation_failed",
                extra={
                    "msg": "主 Agent 取消后传播到 Agent Team 失败",
                    "data": {"parent_run_id": run_id, "end_reason": end_reason},
                },
            )
        # 取消请求必须立即关闭本 Run 的 PTY；workflow 之后仍会协作收束，
        # ``_execute`` 的 finally 还会再次幂等兜底。
        await asyncio.to_thread(self._terminal_session_service.close_run_terminals, run_id, end_reason)
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
            user_decisions: Sequence[UserDecision] = (),
    ) -> None:
        """驱动一次 run 执行；执行器不拥有 run 终态（由 workflow 落定），只负责收尾兜底。

        读取 run → 解析本次 run 独占的 agent profile → 交给 ``run_agent`` 驱动 workflow；
        退出时关闭 terminal 并注销进程内登记。用户输入等待节点直接迁移 Run 状态，本方法在
        资源清理完成前保留该等待态；取消信号不在此清理（由 ``AgentRuntime`` 收尾负责）。

        驱动期异常**不外抛**：``Exception`` 由 workflow 或 ``_converge_unfinished_run`` 兜底；
        ``CancelledError`` 仅置内部标志。等待态在收尾完成前不可由 coordinator 恢复。
        """
        runner_started = False
        try:
            run = self._run_service.get_run(run_id)
            self._terminal_session_service.begin_run(run_id)
            runner_started = True
            runtime = get_runtime()
            agent_profile = runtime.resolve_agent_profile_for_run(run)
            await runtime.run_agent(
                agent_profile,
                execution_mode=start_mode,
                user_decisions=user_decisions,
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
            await asyncio.to_thread(
                self._terminal_session_service.close_run_terminals,
                run_id,
                "run_execution_finished",
            )
            await self._converge_unfinished_run(run_id)
            current = self._executions.get(run_id)
            current_task = asyncio.current_task()
            if current is not None and current.thread_task is current_task:
                self._executions.pop(run_id, None)

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
            run = self._run_service.get_run(run_id)
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
