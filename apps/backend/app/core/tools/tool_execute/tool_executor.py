"""工具执行管线门面：单次工具调用的唯一执行入口。

本模块承载 ``ToolExecutor``——模型请求的工具调用从「准入」到「归一化观察」的完整
管线编排：

    ToolAccessGate（注册表查询 / 参数校验 / Agent 工具白名单 / PreToolUse Hook）
        → FileToolStateCoordinator（prepare / lock / check_stale / complete）
        → ToolHandlerRunner（thread 直跑 或 process 隔离 + 硬超时强杀）
        → PostToolUse Hook
        → ToolObservationBudget（模型通道预算与超限落盘）
        → 工具终态提前投影（``project_tool_terminal_state``，展示旁路）

管线只做编排：每个阶段的实现各自收口在对应协作者中，本模块不含隔离执行细节、
门禁规则或预算算法。门禁拒绝、状态协调提前返回和执行器返回的观察都会经过模型输出预算；
只有通过准入、未在状态准备或 stale 检查阶段提前返回并调用 ``ToolHandlerRunner`` 的路径会触发
PostToolUse Hook，包括 Runner 在 handler 启动前返回取消观察的情况。上层（``WorkflowOperations``）
只依赖 ``execute`` 一个入口，正常返回时拿到 :class:`ToolObservation`。

本模块还承载工具终态向进程内 Transport snapshot 提前投影的唯一落点：观察结果只在
``execute`` 的统一出口投影，门禁拒绝、路径非法、stale 和协调器错误等正常观察都经过该出口；
执行管线抛出异常且提供了执行上下文时，则尝试投影为失败后重新抛出原异常。展示投影失败只
记录日志，不改变工具观察或原异常。
"""

from collections.abc import Collection

from app.core.hook import HookContext, HookEvent, HookInterceptor
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
from app.core.tools.schemas.tool_output import ProcessToolOutputChannel
from app.core.tools.tool_execute.tool_access_gate import ToolAccessGate
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_handler_runner import ToolHandlerRunner
from app.core.tools.tool_execute.tool_observation_budget import ToolObservationBudget
from app.core.tools.tool_execute.tool_terminal_projection import (
    project_tool_terminal_state,
    project_unhandled_tool_failure,
)
from app.core.tools.tool_registry import ToolRegistry


class ToolExecutor:
    """模型请求工具调用的统一执行入口（执行管线门面）。

    单一职责：把「模型请求的一次工具调用」编排为「准入门禁 → 文件状态协调 →
    隔离执行 → PostToolUse Hook → 输出预算」五阶段管线；正常返回时提供归一化的
    :class:`ToolObservation`，使上层（workflow / 运行时）无需关心执行阶段的内部实现。

    职责边界：
    - 负责：按固定时序编排准入门禁、文件状态协调、handler 隔离执行、PostToolUse Hook
      和输出预算。门禁拒绝、状态协调提前返回及执行器观察都经过输出预算；调用
      ``ToolHandlerRunner`` 的路径触发 PostToolUse Hook（包括 handler 启动前取消）。正常观察在
      统一出口投影工具终态；异常路径在提供执行上下文时尝试投影失败，再保留并重新抛出原异常。
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
            state_coordinator: 文件 revision/stale、重复只读调用和写路径锁协作者。
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
            委托文件状态协调器清除该 Task 的 revision 与重复调用状态。路径锁按 workspace
            共享，不会因清理单个 Task 而删除；调用方必须确认该 Task 已没有正在执行的工具调用。
        """

        self._state_coordinator.clear_task(task_id)

    def _prepare_state(
        self,
        tool: ToolDefinition,
        arguments: dict[str, object],
        execution_context: ToolExecutionContext,
        tool_call_id: str,
    ) -> tuple[FileToolExecutionPlan | None, ToolObservation | None]:
        """准备文件状态计划，并对准备阶段的提前返回观察应用输出预算。

        参数:
            tool: 已通过准入门禁的工具定义。
            arguments: 已通过参数校验及 PreToolUse Hook 的最终参数。
            execution_context: 当前工具调用的任务、工作区和运行上下文。
            tool_call_id: 当前模型工具调用标识。

        返回:
            二元组 ``(plan, observation)``。准备成功且无需提前返回时，``plan`` 为执行计划、
            ``observation`` 为 ``None``；文件路径错误或计划携带提前观察时，``observation`` 为
            已经过模型输出预算的失败或提示观察。非文件工具也会得到空状态计划，继续统一执行流程。

        异常:
            文件资源路径错误以及 ``OSError``、``RuntimeError``、``ValueError`` 会在本方法内
            转成 ``ToolObservation``；状态协调器抛出的其他异常会向上传播。

        副作用:
            委托状态协调器解析文件资源、读取相关文件状态并检查重复只读调用；可能更新协调器的
            进程内重复调用状态。提前观察会经过模型输出预算，超限内容可能写入 workspace artifact。
        """

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
        """把状态协调器运行期错误转换为可返回的失败观察。

        参数:
            tool: 当前工具定义，用于填充工具名和权限信息。
            exc: 状态协调阶段捕获到的运行期错误。
            execution_context: 当前工具调用的任务、工作区和运行上下文。
            tool_call_id: 当前模型工具调用标识。

        返回:
            已经过模型输出预算的失败观察；错误标记为可重试。

        异常:
            无。错误观察由 ``tool_error`` 构造并交给输出预算处理。

        副作用:
            可能因输出超限在 workspace 写入完整 artifact。
        """

        return self._budget.apply(
            tool_error(
                tool.name,
                str(exc),
                reason=(
                    f"the file state coordinator could not process the request: {exc}; "
                    "retry after the transient condition clears."
                ),
                retryable=True,
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
        """对 handler 观察触发 PostToolUse Hook，再应用统一输出预算。

        参数:
            tool: 已通过准入门禁的工具定义。
            observation: handler 返回并经文件状态协调处理的观察。
            execution_context: 当前工具调用的任务、工作区和运行上下文。

        返回:
            经模型输出预算处理的观察。Hook 的返回值不替换该观察。

        异常:
            无。Hook 通过 ``HookInterceptor.safe_fire`` 失败安全触发；预算组件按其自身契约处理错误。

        副作用:
            触发 PostToolUse Hook；若观察内容超出预算，可能在 workspace 写入完整 artifact。
        """

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
            # 管线异常时尝试投影失败状态；投影异常不得遮蔽原始异常，也不推断 handler 的外部副作用。
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

        编排顺序：准入门禁（注册表查询 → 参数校验 → Agent 工具白名单 → PreToolUse Hook）
        → 文件状态准备 → 持锁并检查 stale → handler 隔离执行 → 成功时刷新文件状态
        → PostToolUse Hook → 输出预算。非文件工具也经过状态协调器，但会得到空计划，锁、stale
        检查和状态刷新均为空操作。门禁拒绝、文件状态准备提前返回和 stale 观察会经过输出预算，
        不触发 PostToolUse Hook；调用 ``ToolHandlerRunner`` 后都会触发该 Hook，即使 Runner 在
        handler 启动前因取消信号返回观察。

        参数校验、白名单拒绝、路径准备错误和 stale 检查会以 ``ToolObservation`` 返回；handler
        的成功、失败或取消结果也由执行器归一化为观察。状态协调器加锁、检查或完成阶段的
        ``RuntimeError`` 会转成可重试的失败观察。除缺少 ``execution_context`` 的装配错误及其他
        未处理异常外，本方法不主动抛出工具执行结果。

        参数:
            call: 模型请求的工具调用，含工具名、参数与调用 id。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                透传给执行器并由 handler 在执行期消费，便于后续扩展更多执行参数。
            allowed_tool_names: 当前 Agent profile 允许运行的工具名；为 None 表示
                调用方不增加 Agent 级门禁。

        返回:
            归一化后的 :class:`ToolObservation`。成功观察通常为 ``status="success"``；拒绝、
            路径错误、stale 或执行失败通常为 ``status="error"``；取消观察为 ``status="cancelled"``。
            具体错误字段由对应门禁、协调器或 handler 决定。所有正常返回路径的模型可见
            ``content`` 均经过输出预算；``display_data`` 不由该预算器截断。

        异常:
            ValueError: ``execution_context`` 为 None 时抛出；缺失上下文属于装配错误。

        副作用:
            委派 :class:`ToolHandlerRunner` 按 ``execution_mode`` 执行 handler
            （``thread`` 在当前调用线程运行 / ``process`` 在隔离子进程运行并提供硬超时强杀）。
            文件工具可能读取文件状态、获取进程内路径锁并更新进程内 revision/重复调用状态；
            handler 自身还可能产生工具定义的外部副作用。本方法不投影 Transport snapshot；
            正常观察与未处理异常的终态投影均由 :meth:`execute` 负责。
        """
        if execution_context is None:
            raise ValueError("execution_context is required")

        # 阶段一：准入门禁。任一环节拒绝即短路返回（已过预算）。
        gate_outcome = self._gate.evaluate(call, execution_context, allowed_tool_names)
        if not gate_outcome.admitted or gate_outcome.tool is None:
            denial = gate_outcome.denial or self._unknown_denial(call)
            return self._budget.apply(denial, execution_context)
        tool = gate_outcome.tool

        # 阶段二：文件状态协调的准备阶段。准备、预算与提前返回逻辑收口在
        # ``_prepare_state``，使各返回路径的错误文案与边界保持一致。
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

    @staticmethod
    def _unknown_denial(call: ToolCall) -> ToolObservation:
        """构造准入门禁未产出拒绝观察时的兜底 error 观察。

        本方法是防御性兜底：``ToolAccessGate.evaluate`` 的契约保证未准入时提供拒绝观察；
        正常路径不会触达此处。一旦触达，说明门禁结果违反调用方预期，本方法构造确定性失败
        观察，避免 ``None`` 逃逸进上层。

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
