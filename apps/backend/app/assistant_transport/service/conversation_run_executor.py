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
from app.service.depends import get_conversation_run_state_service

ConversationRunRunner = Callable[[ConversationRunRecord], Awaitable[None]]


@dataclass
class _Execution:
    """执行器内部维护的运行条目。

    ``run_id`` 不在此重复存放：它已经是 ``_executions`` 字典的键。
    """

    thread_task: asyncio.Task[None]


class ConversationRunExecutor:
    """在进程内独立驱动一次 ConversationRun。

    本执行器只做「执行」：不拥有 run 终态（``running`` → ``completed`` / ``failed`` /
    ``cancelled`` 由 workflow 落定），也不决定业务准入（由 ``prepare_run_start`` 判定）。
    """

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

        self._run_service = get_conversation_run_state_service()
        self._event_projector = ConversationEventProjector()
        self._signal = cancellation_registry
        self._executions: dict[int, _Execution] = {}

    async def start(self, run_id: int, runner: ConversationRunRunner) -> asyncio.Task[None]:
        """登记 run 并在当前事件循环创建独立后台 task。

        本方法只做「前置条件断言 + 登记 + 建 task」：断言目标 run 已处于 ``running``
        状态（该状态由 ``prepare_run_start`` 在同一 Task 操作闸门内落定），随后把后台
        task 记入进程内 ``_executions``。它不做业务准入决策——「这次请求是否允许启动」
        由 ``prepare_run_start`` 判定；本方法也不落库、不认领 run、不等待执行结果，
        HTTP 订阅断开不会取消该 task。

        参数:
            run_id: 运行对应的 ``ConversationRunRecord.id``。
            runner: ``AgentRuntime.execute_run`` 或同签名适配器；接收
                :class:`ConversationRunRecord`，由后台 task 内的 ``_execute`` 调用。

        返回:
            已创建的后台 ``asyncio.Task``。

        异常:
            KeyError: ``run_id`` 对应的 run 不存在（来自 run service 读取）。
            ValueError: 该 run 当前不是 ``running``（消息为 ``run {run_id} is not
                running``）。这表示调用方违约——未经 ``prepare_run_start`` 置位就调用，
                调用方应视为启动失败并收敛该 run，不得静默继续。

        副作用:
            在当前事件循环创建后台 task 并写入进程内 ``_executions`` 注册；不读取或
            写入 run 状态列。
        """

        run = self._run_service.get_run(run_id)
        if run.status != ConversationRunStatus.RUNNING:
            raise ValueError(f"run {run_id} is not running")
        thread_task = asyncio.create_task(self._execute(run_id, runner))
        self._executions[run_id] = _Execution(thread_task=thread_task)
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

        # 标记是同步集合操作且在第一个 await 之前完成：本调用返回后，该 run 的取消
        # 信号即可被工具与 runner 通过轮询观测到。本方法不落库，因此数据库中的 run
        # 状态不会因这次调用而改变。
        if cancellation_registry.is_cancelled(run_id):
            return False
        self._signal.mark_cancelled(run_id)
        try:
            self._run_service.get_run(run_id)
        except KeyError:
            self._signal.clear(run_id)
            raise
        return True

    async def _execute(
        self,
        run_id: int,
        runner: ConversationRunRunner,
    ) -> None:
        """驱动 runner 执行一次 ConversationRun；执行器不拥有 run 终态。

        本方法只负责「执行」：读取 run 后把控制权交给 ``runner`` 跑完整个 workflow，
        退出时从进程内 ``_executions`` 注销本次执行。它不落库、不落定 run 终态
        （running → completed/failed/cancelled 由 workflow 内部经 run_service 落定），
        也不清理进程内取消信号（该清理由 ``AgentRuntime`` 的收尾负责）。

        参数:
            run_id: 当前运行标识。
            runner: 实际 Agent runtime 执行函数，接收本次 run 记录。

        返回:
            无。

        异常:
            KeyError: ``run_id`` 对应的 run 不存在（来自 run service 读取）。
            runner 抛出的异常在投影工具失败收束后继续向上传播；``start()`` 的调用方
                不 await 该 task，因此异常按 asyncio「未取回的 task 异常」语义处理 ⇒
                run 的终态必须在 workflow 抛出之前由 workflow 自行落定。

        副作用:
            runner 抛 ``Exception`` 时先经 ``_project_tools_settled`` 投影工具失败收束
            （该投影失败只记日志，不替换原始异常）；退出时若本次执行仍在 ``_executions``
            中登记且当前 task 就是登记的那个 task，则移除该登记；不改写 run 状态列、
            不清理取消信号。
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

        这是 Run 终态提交后的 Transport 旁路；``status`` 非 ``cancelled`` 时统一按
        ``failed`` 投影。projector 未装配时静默返回；任何读取或投影失败都只记录 error
        日志，不得把已收束的 workflow 再次打回 active/failed 异常路径。
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

