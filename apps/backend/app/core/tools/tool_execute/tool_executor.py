"""工具执行管线门面：单次工具调用的唯一执行入口。

本模块承载 ``ToolExecutor``——模型请求的工具调用从「准入」到「归一化观察」的完整
管线编排：

    ToolAccessGate（注册表命中 / 权限门禁 / 参数校验 / PreToolUse Hook）
        → FileToolStateCoordinator（prepare / lock / check_stale / complete）
        → ToolHandlerRunner（thread 直跑 或 process 隔离 + 硬超时强杀）
        → PostToolUse Hook
        → ToolObservationBudget（模型通道预算与超限落盘）
        → 工具终态提前投影（``project_tool_terminal_state``，展示旁路）

管线只做编排：每个阶段的实现各自收口在对应协作者中，本模块不含隔离执行细节、
权限文案或预算算法。上层（``WorkflowOperations``）只依赖 ``execute`` 一个入口，
拿到的始终是已治理、可落库、可回传的 :class:`ToolObservation`。

本模块还承载「一次工具调用跑完就立刻把终态推给前端」的唯一落点：投影只写在
``execute`` 的单一出口，不在十几条 early return 上各加一次调用，使门禁拒绝、路径非法、
stale、协调器异常等分支自动覆盖，将来新增分支也不会漏。
"""

import asyncio
from collections.abc import Callable, Collection

from app.core.hook import HookContext, HookEvent, HookInterceptor
from app.core.runtime.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.tools.guard.file_resource_paths import FileResourcePathError
from app.core.tools.guard.file_tool_state_coordinator import (
    FileToolExecutionPlan,
    FileToolStateCoordinator,
)
from app.core.tools.guard.tool_output_budget import ToolOutputBudget
from app.core.tools.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_access_gate import ToolAccessGate
from app.core.tools.tool_execute.tool_cancelled import ToolCallCancelled, tool_cancelled
from app.core.tools.tool_execute.tool_error import handler_exception_reason, tool_error
from app.core.tools.tool_execute.tool_handler_runner import ToolHandlerRunner
from app.core.tools.tool_execute.tool_observation_budget import ToolObservationBudget
from app.core.tools.tool_execute.tool_terminal_projection import (
    project_tool_terminal_state,
    project_unhandled_tool_failure,
)
from app.core.tools.tool_registry import ToolRegistry

_ASYNC_CANCELLATION_POLL_INTERVAL_SECONDS = 0.05


class ToolExecutor:
    """模型请求工具调用的统一执行入口（执行管线门面）。

    单一职责：把「模型请求的一次工具调用」编排为「准入门禁 → 文件状态协调 →
    隔离执行 → PostToolUse Hook → 输出预算」五阶段管线，并始终返回归一化的
    :class:`ToolObservation`，使上层（workflow / 运行时）无需关心失败原因细节。

    职责边界：
    - 负责：按固定时序编排五个阶段，保证**每条**返回路径（含门禁拒绝、状态协调
      提前返回、执行失败）都过同一道输出预算，并在单一出口对**每条**返回路径、以及
      管线自身抛出的未归一化异常，各做一次工具终态提前投影。
    - 不负责：门禁判定规则（``ToolAccessGate``）、文件 revision/stale/锁机制
      （``FileToolStateCoordinator``）、子进程隔离与超时强杀
      （``ToolHandlerRunner``）、预算算法（``ToolObservationBudget``）、
      终态映射与投影实现（``tool_terminal_projection``）、handler 业务逻辑、
      模型可见性之外的运行策略。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        state_coordinator: FileToolStateCoordinator | None = None,
        output_budget: ToolOutputBudget | None = None,
    ) -> None:
        """初始化执行管线并装配各阶段协作者。

        参数:
            registry: 工具注册表，提供工具定义查询。
            state_coordinator: 文件 revision、重复调用和路径锁协作者。
            output_budget: 模型可见 ``content`` 的统一输出预算。

        返回:
            无。

        异常:
            无。

        副作用:
            持有 ``registry``、``gate``、``state_coordinator``、``runner``、
            ``budget`` 引用；不触发任何工具执行。
        """

        self._registry = registry
        self._gate = ToolAccessGate(registry)
        self._state_coordinator = state_coordinator or FileToolStateCoordinator()
        self._runner = ToolHandlerRunner()
        self._budget = ToolObservationBudget(output_budget)

    def list_tools(self) -> list[ToolDefinition]:
        """返回全部已注册工具定义。

        参数:
            无。

        返回:
            注册表中全部 :class:`ToolDefinition` 的列表。

        异常:
            无。

        副作用:
            无（只读注册表）。
        """

        return self._registry.get_all_definitions()

    def get_tool_definition(self, tool_name: str) -> ToolDefinition | None:
        """按名称查询已注册的工具定义。

        参数:
            tool_name: 工具名称。

        返回:
            命中的 :class:`ToolDefinition`；未注册时返回 None。

        异常:
            无。

        副作用:
            无（只读注册表）。
        """

        return self._registry.get_tool_definition(tool_name)

    def clear_task_state(self, task_id: int) -> None:
        """清除指定 Task 的进程内文件工具状态。

        参数:
            task_id: 待清除任务标识。

        返回:
            无。

        异常:
            无。

        副作用:
            委托文件状态协调器清除 revision、路径锁和重复调用状态；调用方必须
            确认该 Task 已没有正在执行的工具调用。
        """

        self._state_coordinator.clear_task(task_id)

    def _prepare_state(
        self,
        tool: ToolDefinition,
        arguments: dict[str, object],
        execution_context: ToolExecutionContext,
        tool_call_id: str,
    ) -> tuple[FileToolExecutionPlan | None, ToolObservation | None]:
        """Run the shared file-state preparation and budget early exits."""

        try:
            plan = self._state_coordinator.prepare(
                tool,
                arguments,
                execution_context,
                tool_call_id=tool_call_id,
            )
        except FileResourcePathError as exc:
            return None, self._budget.apply(
                tool_error(
                    tool.name,
                    str(exc),
                    reason=exc.reason,
                    permission=tool.permission,
                    tool_call_id=tool_call_id,
                ),
                execution_context,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return None, self._budget.apply(
                tool_error(
                    tool.name,
                    f"invalid file path: {exc}",
                    reason=(
                        f"the file path is invalid: {exc}; pass a well-formed path inside "
                        "the project before retrying."
                    ),
                    permission=tool.permission,
                    tool_call_id=tool_call_id,
                ),
                execution_context,
            )
        if plan.early_observation is not None:
            return plan, self._budget.apply(plan.early_observation, execution_context)
        return plan, None

    def _state_coordinator_error(
        self,
        tool: ToolDefinition,
        exc: RuntimeError,
        execution_context: ToolExecutionContext,
        tool_call_id: str,
    ) -> ToolObservation:
        """Normalize a shared state-coordinator runtime failure."""

        return self._budget.apply(
            tool_error(
                tool.name,
                str(exc),
                reason=(
                    f"the file state coordinator could not process the request: {exc}; "
                    "retry after the transient condition clears."
                ),
                retryable=True,
                permission=tool.permission,
                tool_call_id=tool_call_id,
            ),
            execution_context,
        )

    def _finish_observation(
        self,
        tool: ToolDefinition,
        observation: ToolObservation,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """Run the shared post-hook and observation-budget completion stages."""

        HookInterceptor.safe_fire(
            HookContext.from_locatable(
                event=HookEvent.POST_TOOL_USE,
                locatable=execution_context,
                tool_name=tool.name,
                tool_observation=observation,
            )
        )
        return self._budget.apply(observation, execution_context)

    def execute(
        self,
        call: ToolCall,
        execution_context: ToolExecutionContext | None = None,
        allowed_tool_names: Collection[str] | None = None,
    ) -> ToolObservation:
        """执行单次工具调用、投影其终态，并返回归一化观察结果。

        这是执行管线的唯一出口：先经 :meth:`_execute_inner` 完成五阶段编排，再把该观察的
        终态提前投影到进程内 Transport snapshot（``completed`` / ``failed`` /
        ``cancelled``），最后原样返回观察。投影写在唯一出口而非各 early return 处，门禁
        拒绝、路径非法、stale、协调器异常等分支自动覆盖。

        参数:
            call: 模型请求的工具调用，含工具名、参数与调用 id。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；为 None 时
                由 :meth:`_execute_inner` 判定为装配错误并抛出。
            allowed_tool_names: 当前 Agent profile 允许运行的工具名；为 None 表示
                调用方不增加 Agent 级门禁。

        返回:
            与 :meth:`_execute_inner` 相同的归一化 :class:`ToolObservation`（投影只读它，
            不修改任何字段）。

        异常:
            ValueError: ``execution_context`` 为 None 时抛出（见 :meth:`_execute_inner`），
                该路径不产生观察因而不触发终态投影。
            Exception: :meth:`_execute_inner` 抛出的任何其它异常都会原样上抛；上抛前已把该
                调用投影为 ``failed`` 终态，避免前端停留在 ``running``。上游
                ``WorkflowOperations`` 随后自行把该异常归一化为内部错误观察。

        副作用:
            除 :meth:`_execute_inner` 的副作用外，额外把工具终态直投进进程内 snapshot；
            投影失败只记日志、不改写观察（展示层缺口不得中断工具执行链路）。
        """

        try:
            observation = self._execute_inner(call, execution_context, allowed_tool_names)
        except Exception:
            # 管线自身异常：该调用已确定性结束且失败。先投终态再上抛，投影失败也不遮蔽原异常。
            if execution_context is not None:
                project_unhandled_tool_failure(
                    task_id=execution_context.task_id,
                    run_id=execution_context.run_id,
                    tool_call_id=call.call_id or execution_context.tool_call_id,
                )
            raise
        if execution_context is not None:
            project_tool_terminal_state(
                task_id=execution_context.task_id,
                run_id=execution_context.run_id,
                # 与取消通知同口径：归一化后的 call_id 才可能命中事件契约，空串由投影内部跳过。
                tool_call_id=call.call_id or execution_context.tool_call_id,
                observation=observation,
            )
        return observation

    def _execute_inner(
        self,
        call: ToolCall,
        execution_context: ToolExecutionContext | None = None,
        allowed_tool_names: Collection[str] | None = None,
    ) -> ToolObservation:
        """执行单次工具调用并返回归一化观察结果（不含 Transport 终态投影）。

        编排顺序：准入门禁（注册表命中 → 权限门禁 → 参数校验 → PreToolUse Hook）
        → 文件工具经状态协调（revision/stale/锁）后委派隔离执行器，非文件工具由
        协调器短路为空计划直接执行 → PostToolUse Hook → 输出预算；
        任一前置环节失败都直接返回带 ``reason`` 的 :class:`ToolObservation`，使上层始终拿到
        可落库/可回传的结果；除 ``execution_context`` 缺失这一装配错误外不抛出。

        参数:
            call: 模型请求的工具调用，含工具名、参数与调用 id。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                透传给执行器并由 handler 在执行期消费，便于后续扩展更多执行参数。
            allowed_tool_names: 当前 Agent profile 允许运行的工具名；为 None 表示
                调用方不增加 Agent 级门禁。

        返回:
            归一化后的 :class:`ToolObservation`：成功为 status="success"；
            未知工具 / 权限拒绝 / 参数非法 / 执行失败为 status="error"，
            每种失败均经 :func:`tool_error` 构造，``reason`` 为面向模型的
            富文本（根因 + 修正建议 + 与 ``retryable`` 一致的重试提示），
            而非稳定机器短码。所有返回路径均已过双通道输出预算。

        异常:
            ValueError: ``execution_context`` 为 None 时抛出——执行链强制注入上下文，
                缺失即装配错误，不做静默降级。

        副作用:
            委派 :class:`ToolHandlerRunner` 启动子进程执行；可能因权限或参数
            校验失败而短路返回，不进入执行阶段。本方法不动 Transport snapshot；
            终态投影由 :meth:`execute` 的单一出口负责。
        """
        if execution_context is None:
            raise ValueError("execution_context is required")

        # 阶段一：准入门禁。任一环节拒绝即短路返回（已过预算）。
        gate_outcome = self._gate.evaluate(call, execution_context, allowed_tool_names)
        if not gate_outcome.admitted or gate_outcome.tool is None:
            denial = gate_outcome.denial or self._unknown_denial(call)
            return self._budget.apply(denial, execution_context)
        tool = gate_outcome.tool
        if tool.handler_kind != "sync":
            return self._budget.apply(
                tool_error(
                    tool.name,
                    "async tool handler requires ToolExecutor.execute_async",
                    reason=(
                        "this tool is explicitly asynchronous and cannot be dispatched by the "
                        "synchronous execution entry point; use the async workflow path."
                    ),
                    permission=tool.permission,
                    tool_call_id=call.call_id,
                ),
                execution_context,
            )

        # 阶段二：文件状态协调的准备阶段。同步与异步入口共享同一准备、预算和
        # early-return node_helper，避免两条管线的错误文案与边界逐渐漂移。
        plan, early_observation = self._prepare_state(
            tool,
            gate_outcome.arguments,
            execution_context,
            call.call_id,
        )
        if early_observation is not None:
            return early_observation
        assert plan is not None

        # 阶段三：持锁 → stale 检查 → 隔离执行 → 状态回写。
        try:
            with self._state_coordinator.lock(plan, execution_context):
                stale_observation = self._state_coordinator.check_stale(
                    plan,
                    tool,
                    execution_context,
                    tool_call_id=call.call_id,
                )
                if stale_observation is not None:
                    return self._budget.apply(stale_observation, execution_context)

                def run_handler() -> ToolObservation:
                    return self._runner.execute(
                        tool,
                        gate_outcome.arguments,
                        execution_context=execution_context,
                        tool_call_id=call.call_id,
                    )

                observation = run_handler()
                self._state_coordinator.complete(
                    plan,
                    observation,
                    execution_context,
                )
        except RuntimeError as exc:
            return self._state_coordinator_error(tool, exc, execution_context, call.call_id)

        # 阶段四、五：PostToolUse 与统一输出预算的共享完成阶段。
        return self._finish_observation(tool, observation, execution_context)

    async def execute_async(
        self,
        call: ToolCall,
        execution_context: ToolExecutionContext | None = None,
        allowed_tool_names: Collection[str] | None = None,
    ) -> ToolObservation:
        """Execute one explicitly async tool on the current event loop.

        The access gate, file-state boundary, hooks, observation budget, and terminal
        projection are the same as :meth:`execute`; only handler dispatch differs.
        Synchronous definitions are rejected instead of being silently awaited or moved
        to a worker thread.
        """

        if execution_context is None:
            raise ValueError("execution_context is required")

        try:
            tool = self._registry.get_tool_definition(call.tool_name)
            if tool is not None and tool.handler_kind != "async":
                raise TypeError(
                    f"tool '{tool.name}' declares handler_kind=sync; "
                    "ToolExecutor.execute_async requires handler_kind=async"
                )

            try:
                observation = await self._execute_inner_async(
                    call, execution_context, allowed_tool_names
                )
            except asyncio.CancelledError:
                # Task cancellation is a control-flow signal, not a handler failure.  Project
                # the same terminal observation used by the synchronous runner, then preserve
                # asyncio's cancellation contract for the workflow caller.
                definition = self._registry.get_tool_definition(call.tool_name)
                cancelled = tool_cancelled(
                    call.tool_name,
                    permission=definition.permission if definition is not None else "",
                    tool_call_id=call.call_id,
                )
                project_tool_terminal_state(
                    task_id=execution_context.task_id,
                    run_id=execution_context.run_id,
                    tool_call_id=call.call_id or execution_context.tool_call_id,
                    observation=cancelled,
                )
                if tool_call_cancellation_registry.is_cancelled(
                    execution_context.run_id, call.call_id
                ):
                    raise ToolCallCancelled from None
                raise
            except Exception:
                project_unhandled_tool_failure(
                    task_id=execution_context.task_id,
                    run_id=execution_context.run_id,
                    tool_call_id=call.call_id or execution_context.tool_call_id,
                )
                raise
            project_tool_terminal_state(
                task_id=execution_context.task_id,
                run_id=execution_context.run_id,
                tool_call_id=call.call_id or execution_context.tool_call_id,
                observation=observation,
            )
            return observation
        finally:
            self._runner.cleanup_cancellation_signal(execution_context, call.call_id)

    @staticmethod
    async def _wait_for_cancellation(should_cancel: Callable[[], bool]) -> None:
        """Poll a live cancellation check without blocking the event loop."""

        while not should_cancel():  # noqa: ASYNC110 - registry has no awaitable signal
            await asyncio.sleep(_ASYNC_CANCELLATION_POLL_INTERVAL_SECONDS)

    @staticmethod
    async def _await_cancelled_task(task: asyncio.Task[object]) -> None:
        """Wait for a task after requesting cancellation, consuming its CancelledError."""

        await asyncio.gather(task, return_exceptions=True)

    async def _run_async_handler(
        self,
        tool: ToolDefinition,
        arguments: dict[str, object],
        execution_context: ToolExecutionContext,
        tool_call_id: str,
        should_cancel: Callable[[], bool] | None,
    ) -> object:
        """Run an async handler and turn registry cancellation into task cancellation.

        The handler and registry watcher are independent tasks.  Registry cancellation
        cancels and awaits the handler task before propagating ``CancelledError``; outer
        task cancellation follows the same cleanup path.  All waits are asyncio awaits,
        so no event-loop thread is blocked.
        """

        if should_cancel is not None and should_cancel():
            raise asyncio.CancelledError

        handler_task = asyncio.create_task(
            tool.handler(**arguments, execution_context=execution_context)
        )
        if should_cancel is None:
            return await handler_task

        cancellation_task = asyncio.create_task(self._wait_for_cancellation(should_cancel))
        try:
            done, _ = await asyncio.wait(
                {handler_task, cancellation_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done:
                handler_task.cancel()
                await self._await_cancelled_task(handler_task)
                raise asyncio.CancelledError

            payload = handler_task.result()
            if should_cancel():
                raise asyncio.CancelledError
            return payload
        except asyncio.CancelledError:
            if not handler_task.done():
                handler_task.cancel()
            await self._await_cancelled_task(handler_task)
            raise
        finally:
            if not cancellation_task.done():
                cancellation_task.cancel()
            await self._await_cancelled_task(cancellation_task)

    async def _execute_inner_async(
        self,
        call: ToolCall,
        execution_context: ToolExecutionContext,
        allowed_tool_names: Collection[str] | None = None,
    ) -> ToolObservation:
        """Run the common tool pipeline with direct async handler dispatch."""

        gate_outcome = self._gate.evaluate(call, execution_context, allowed_tool_names)
        if not gate_outcome.admitted or gate_outcome.tool is None:
            denial = gate_outcome.denial or self._unknown_denial(call)
            return self._budget.apply(denial, execution_context)
        tool = gate_outcome.tool
        if tool.handler_kind != "async":
            raise TypeError(
                f"tool '{tool.name}' declares handler_kind=sync; "
                "ToolExecutor.execute_async requires handler_kind=async"
            )

        plan, early_observation = self._prepare_state(
            tool,
            gate_outcome.arguments,
            execution_context,
            call.call_id,
        )
        if early_observation is not None:
            return early_observation
        assert plan is not None

        try:
            with self._state_coordinator.lock(plan, execution_context):
                stale_observation = self._state_coordinator.check_stale(
                    plan,
                    tool,
                    execution_context,
                    tool_call_id=call.call_id,
                )
                if stale_observation is not None:
                    return self._budget.apply(stale_observation, execution_context)
                try:
                    should_cancel = self._runner.build_cancellation_check(
                        execution_context, call.call_id
                    )
                    payload = await self._run_async_handler(
                        tool,
                        gate_outcome.arguments,
                        execution_context,
                        call.call_id,
                        should_cancel,
                    )
                except Exception as exc:
                    observation = tool_error(
                        tool.name,
                        str(exc),
                        reason=handler_exception_reason(
                            f"the async tool handler raised an exception: {exc}"
                        ),
                        permission=tool.permission,
                        tool_call_id=call.call_id,
                    )
                else:
                    observation = self._runner.normalize_result(tool, payload, call.call_id)
                self._state_coordinator.complete(plan, observation, execution_context)
        except RuntimeError as exc:
            return self._state_coordinator_error(tool, exc, execution_context, call.call_id)

        return self._finish_observation(tool, observation, execution_context)

    @staticmethod
    def _unknown_denial(call: ToolCall) -> ToolObservation:
        """构造准入门禁未产出拒绝观察时的兜底 error 观察。

        本方法是防御性兜底：``ToolAccessGate.evaluate`` 的契约保证「未准入时
        ``denial`` 非空」，正常路径不会触达此处；一旦触达说明门禁契约被破坏，
        返回一条确定性 error 观察，避免 None 逃逸进上层。

        参数:
            call: 触发兜底的工具调用。

        返回:
            面向模型的确定性 error 观察。

        异常:
            无。

        副作用:
            无（仅构造并返回新对象）。
        """

        return tool_error(
            call.tool_name,
            f"tool access gate denied the call without a reason: {call.tool_name}",
            reason=(
                f"the tool '{call.tool_name}' was rejected by the access gate without "
                f"a specific reason; this is an internal runtime failure rather than a "
                f"tool-reported error, so retrying with identical arguments will fail "
                f"again until the runtime issue is fixed."
            ),
            retryable=False,
            tool_call_id=call.call_id,
        )
