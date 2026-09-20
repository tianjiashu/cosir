"""进程内 ConversationRun 后台执行器。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.assistant_transport.event import ToolCallsSettledEvent
from app.assistant_transport.service.conversation_event_projector import ConversationEventProjector
from app.config.logging.logger import log
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.models import ConversationRunRecord, ConversationRunStatus
from app.service.depends import get_conversation_run_state_service, get_delegation_service

ConversationRunRunner = Callable[[ConversationRunRecord], Awaitable[None]]


@dataclass
class _Execution:
    """执行器内部维护的运行条目。

    ``run_id`` 不在此重复存放：它已经是 ``_executions`` 字典的键。
    """

    thread_task: asyncio.Task[None]


class ConversationRunExecutor:
    """在进程内独立驱动一次 ConversationRun，并作为进程内取消信号的标记入口。

    本执行器只做「执行」：不拥有 run 终态（``running`` → ``completed`` / ``failed`` /
    ``cancelled`` 由 workflow 落定），也不决定业务准入（由 ``prepare_run_start`` 判定）。

    取消入口同属本类但只负责发信号、不负责收束：``cancel`` 标记 run 级取消（整个 run
    停止，并沿委派关系级联到后代 run），``cancel_tool_call`` 标记工具级取消（只中止一次
    工具调用，run 继续）。两者的终态都由各自的消费方落定，信号本身不落库、不跨进程、
    不跨重启。
    """

    def __init__(
        self,
    ) -> None:
        """初始化执行器：装配 run_service、event_projector、进程内取消信号源与空运行注册表。

        返回:
            无。

        副作用:
            从依赖装配取得 ``run_service`` / ``event_projector`` / ``delegation_service`` /
            进程内取消信号源；初始化空的进程内运行注册表；不读取或写入任何 run 状态。
        """

        self._run_service = get_conversation_run_state_service()
        self._event_projector = ConversationEventProjector()
        self._delegation_service = get_delegation_service()
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
            在当前事件循环创建后台 task、为其挂异常记录回调并写入进程内 ``_executions``
            注册；不读取或写入 run 状态列。
        """

        run = self._run_service.get_run(run_id)
        if run.status != ConversationRunStatus.RUNNING:
            raise ValueError(f"run {run_id} is not running")
        thread_task = asyncio.create_task(self._execute(run_id, runner))
        # 后台 task 无人 await：不挂回调时其异常会被 asyncio 静默吞掉，排障只能靠间接日志。
        thread_task.add_done_callback(lambda task: self._log_execution_result(run_id, task))
        self._executions[run_id] = _Execution(thread_task=thread_task)
        return thread_task

    @staticmethod
    def _log_execution_result(run_id: int, task: asyncio.Task[None]) -> None:
        """记录后台执行 task 的异常结束，避免异常在事件循环里无痕消失。

        本方法只做「取回并记录 task 结果」这件事：它不改变 run 状态、不重试、不重新抛出，
        也不替换 workflow 已经落定的终态。

        参数:
            run_id: 本次后台执行对应的 Conversation Run 标识。
            task: ``start`` 创建的后台 task。

        返回:
            无。

        异常:
            无。task 已被取消或正常结束时直接返回；异常 inspection 自身失败按静默返回处理。

        副作用:
            异常结束时写 ``conversation_run_execution_failed``（ERROR，含异常类型与异常文本）
            日志；正常结束或取消不写日志。
        """

        if task.cancelled():
            return
        try:
            error = task.exception()
        except Exception:  # 结果取回失败不能反过来打断事件循环回调
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
        """标记指定 run 及其后代 run 的进程内取消信号；不落库 run 终态、不直接中断后台 task。

        本方法只负责「发送取消信号」一件事：在进程内取消信号源标记 ``run_id``，使工具
        /runner 在轮询信号时能协作停止。run 的终态转移（active → cancelled）由 workflow
        经 run_service 负责，本方法不触碰 run 状态列；后台 asyncio task 也不直接取消，
        而是由 runner 观察到信号后自行收束（全局收口由 ``close`` 统一取消）。

        存在性预检：mark 之后立刻确认 run 存在，不存在则回滚已标记的信号再抛
        ``KeyError``，保持「信号已标记」与「run 真实存在」的一致性。mark 与存在性检查之间
        无 ``await``，事件循环不会插入其他协程，避免「取消已标记但 run 不存在」的竞态窗口。

        级联：确认 run 存在后，把取消请求沿委派关系传播到它的全部后代 run（子 Agent 的
        run），否则父 run 被取消时子 Agent 会继续跑到自己的终态。级联同样只发信号；级联
        抛出的 ``Exception`` 只记 error 日志，不影响本方法的返回值与父 run 的取消（需要
        中断进程的 ``BaseException`` 例外，仍向外传播）。

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
            向进程内取消信号源写入 ``run_id`` 及其全部后代 run；run 不存在时回滚该信号；
            不发起状态落库、不取消 asyncio task。HTTP 订阅断开不会调用本方法。
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
        # 级联放在存在性确认之后：不存在的 run 不可能有后代 run，同时避免 404 路径白查一次库。
        try:
            self._cancel_descendant_runs(run_id)
        except Exception:
            # 级联是附加传播：它失败不影响父 run 收口，因此**不允许**让用户的取消请求失败
            # （父信号此时已标记，父 run 仍会按既有链路收口）。
            log.exception(
                "run_cancellation_cascade_failed",
                extra={
                    "msg": "取消级联失败，父 run 的取消请求已生效",
                    "data": {"parent_run_id": run_id},
                },
            )
        return True

    async def cancel_tool_call(self, run_id: int, tool_call_id: str) -> bool:
        """标记指定工具调用的进程内取消信号；不落库、不中断 run 或后台 task。

        本方法只负责「发送工具级取消信号」一件事：在 ``tool_call_cancellation_registry``
        标记 ``(run_id, tool_call_id)``，使该次工具调用在轮询信号时协作停止。它不改变
        run 生命周期、Agent 执行与工具观察：工具执行层检出信号后返回 ``cancelled`` 观察，
        run 终态与模型侧 ``ToolMessage`` 仍由 workflow 落定；由于不落库，数据库中该 run
        仍是 ``running``，前端需继续以 canonical snapshot 为准。

        标记前先确认 run 存在（存在性预检），不存在则回滚已标记的信号再抛 ``KeyError``，
        保持「信号已标记」与「run 真实存在」的一致性。mark 与存在性检查之间无 ``await``，
        事件循环不会插入其他协程，避免「取消已标记但 run 不存在」的竞态窗口。

        参数:
            run_id: 目标工具调用所属的 Conversation Run 标识。
            tool_call_id: 目标工具调用标识（模型工具调用 id）。

        返回:
            信号为新标记（取消请求已发出）返回 ``True``；该工具调用此前已标记取消返回
            ``False``。工具执行层在单次调用结束时释放该信号，run 收尾时按 run_id 兜底
            清理，因此对已结束的调用再次调用本方法会重新返回 ``True``。

        异常:
            KeyError: ``run_id`` 对应的 run 不存在——先撤销已标记的信号再向上传播，
                由 API 层映射为 404。

        副作用:
            向进程内工具级取消信号源写入 ``(run_id, tool_call_id)``；run 不存在时回滚该
            信号；不发起状态落库、不取消 asyncio task、不触碰 run 级取消信号。
        """

        # 标记是同步集合操作且在第一个 await 之前完成：本调用返回后，该工具调用的取消
        # 信号即可被工具执行层通过轮询观测到。本方法不落库，因此数据库中的 run 状态与
        # 工具调用状态都不会因这次调用而改变。
        if tool_call_cancellation_registry.is_cancelled(run_id, tool_call_id):
            return False
        tool_call_cancellation_registry.mark_cancelled(run_id, tool_call_id)
        try:
            self._run_service.get_run(run_id)
        except KeyError:
            tool_call_cancellation_registry.clear(run_id, tool_call_id)
            raise
        return True

    def _cancel_descendant_runs(self, run_id: int) -> None:
        """把取消请求沿委派关系传播到全部后代 run（只发信号）。

        子 Agent 的 run 不会因为父 run 被取消而自动停止（委派桥接器只观察 child run 自身的
        信号），因此这里按 ``delegations`` 记录把取消请求传播下去：后代 run 的信号一旦标记，
        它自己的 workflow 就会在下一个检查点收口。

        遍历：广度优先 + visited 防环。当前委派策略把深度限制为 1，但本方法不依赖该策略，
        策略放宽后自动覆盖更深的后代，也不会因数据异常成环而无限循环。

        参数:
            run_id: 已被请求取消的 run 标识；其自身不参与标记（调用方已标记）。

        返回:
            无。

        异常:
            查询或标记过程中的异常一律向上抛出，由 :meth:`cancel` 的边界兜底记录——本方法
            不做二次吞异常，避免同一失败出现两套处理策略。

        副作用:
            为每个后代 run 写入进程内 run 级取消信号，并写
            ``run_cancellation_cascade_applied``（INFO）日志；不落库、不修改 ``delegations``
            记录、不取消 asyncio task。
        """

        visited: set[int] = {run_id}
        queue: deque[int] = deque([run_id])
        cancelled_child_run_ids: list[int] = []
        while queue:
            current_run_id = queue.popleft()
            for record in self._delegation_service.list_active_by_parent_turn(current_run_id):
                child_run_id = record.child_run_id
                if not child_run_id or child_run_id in visited:
                    continue
                visited.add(child_run_id)
                queue.append(child_run_id)
                cancellation_registry.mark_cancelled(child_run_id)
                cancelled_child_run_ids.append(child_run_id)
        if cancelled_child_run_ids:
            log.info(
                "run_cancellation_cascade_applied",
                extra={
                    "msg": "父 run 取消已级联到子 Agent run",
                    "data": {
                        "parent_run_id": run_id,
                        "child_run_count": len(cancelled_child_run_ids),
                        "child_run_ids": cancelled_child_run_ids,
                    },
                },
            )

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

