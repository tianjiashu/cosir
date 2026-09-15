"""进程内 ConversationRun 后台执行器。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.assistant_transport.event import ToolCallsSettledEvent
from app.assistant_transport.service.conversation_event_projector import ConversationEventProjector
from app.config.logging.logger import log
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.models import ConversationRunRecord, ConversationRunStatus
from app.service.depends import get_conversation_run_service
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

ConversationRunRunner = Callable[[ConversationRunRecord], Awaitable[None]]
CANCEL_WAIT_TIMEOUT_SECONDS = 5.0


@dataclass
class _Execution:
    """执行器内部维护的运行条目。"""

    run_id: int
    thread_task: asyncio.Task[None]


class ConversationRunExecutor:
    """在进程内独立驱动一次 ConversationRun。"""

    def __init__(
        self,
    ) -> None:
        """初始化执行器：装配 run_service、event_projector、进程内取消信号源与空运行注册表。

        返回:
            无。

        副作用:
            从依赖装配取得 ``run_service`` / ``event_projector`` / 进程内取消信号源；
            初始化空的进程内运行注册表；不读取或写入任何 run 状态。
        """

        self._run_service = get_conversation_run_service()
        self._event_projector = ConversationEventProjector()
        self._signal = cancellation_registry
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

        run = self._run_service.get_run(run_id)
        if run.status != ConversationRunStatus.RUNNING:
            raise ValueError(f"run {run_id} is not running")
        thread_task = asyncio.create_task(self._execute(run_id, runner))
        self._executions[run_id] = _Execution(run_id=run_id, thread_task=thread_task)
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
        """标记指定 run 的进程内取消信号；不落库 run 终态、不直接中断后台 task。

        本方法只负责「发送取消信号」一件事：在进程内取消信号源标记 ``run_id``，使工具
        /runner 在轮询信号时能协作停止。run 的终态转移（active → cancelled）由 workflow
        经 run_service 负责，本方法不触碰 run 状态列；后台 asyncio task 也不直接取消，
        而是由 runner 观察到信号后自行收束（全局收口由 ``close`` 统一取消）。

        标记前先确认 run 存在（存在性预检），不存在则回滚已标记的信号再抛 ``KeyError``，
        保持「信号已标记」与「run 真实存在」的一致性。mark 与存在性检查之间无 ``await``，
        事件循环不会插入其他协程，避免「取消已标记但 run 不存在」的竞态窗口。

        参数:
            run_id: 待取消的运行标识。
            end_reason: 保留形参，本方法不消费（run 终态的 end_reason 由 workflow 落定），
                保留以兼容调用方签名。

        返回:
            信号为新标记（取消请求已发出）返回 ``True``；run 已被标记取消返回 ``False``。

        异常:
            KeyError: ``run_id`` 对应的 run 不存在——先撤销已标记的信号再向上传播，
                由 API 层映射为 404。

        副作用:
            向进程内取消信号源写入 ``run_id``；run 不存在时回滚该信号；不发起状态落库、
            不取消 asyncio task。HTTP 订阅断开不会调用本方法。
        """

        # 这是同步集合操作，且在第一个 await 之前完成。恢复入口可以据此把
        # “取消已开始但数据库尚未切换”的短窗口视为冲突，避免旧 task 与新
        # resume task 交叉运行同一个 task 的 context / tool / snapshot。
        if cancellation_registry.is_cancelled(run_id):
            return False
        self._signal.mark_cancelled(run_id)
        try:
            self._run_service.get_run(run_id)
        except KeyError:
            self._signal.clear(run_id)
            raise
        return True

    def is_locally_running(self, run_id: int) -> bool:
        try:
            return self._run_service.get_run(run_id).status == ConversationRunStatus.RUNNING
        except KeyError:
            return False

    async def _execute(
        self,
        run_id: int,
        runner: ConversationRunRunner,
    ) -> None:
        """驱动 runner 执行一次 ConversationRun；执行器不拥有 run 终态。

        本方法只负责「执行」：在 Task 操作闸门内认领 run 并调用 ``runner`` 跑完整个
        workflow，退出时清理执行注册与进程内取消信号。run 的终态转移
        （running → completed/failed/cancelled）由 workflow 内部经 run_service 落定，
        本方法不调用 run_service 改写 run 状态列，避免与 workflow 重复落定；执行异常
        由 runner 自身按 run_service 的 ``if_running`` 守卫条件落定 failed。

        参数:
            run_id: 当前运行标识。
            run: 已认领的运行记录。
            runner: 实际 Agent runtime 执行函数。

        返回:
            无。

        异常:
            ValueError: 该 run 已被其它执行器认领（``claim_or_resume_run`` 返回 False）。
            runner 抛出的异常继续向上传播（后台 task 内由 asyncio 捕获），供 ``finally`` 清理。

        副作用:
            经 ``claim_or_resume_run`` 做同一 Task 内 Run 的认领栅栏（DB 级互斥，无进程内锁）；
            退出时移除执行注册并清理进程内取消信号（清理唯一收口，覆盖取消/失败/完成全部路径）；
            不改写 run 终态。
        """
        try:
            run = self._run_service.get_run(run_id)
            try:
                await runner(run)
            except Exception as e:
                self._project_tools_settled(run_id, "failed", "runtime_failed")
                raise e
        finally:
            current = self._executions.get(run_id)
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if current is not None and current.thread_task is current_task:
                self._executions.pop(run_id, None)

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

