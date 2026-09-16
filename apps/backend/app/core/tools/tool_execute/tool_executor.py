"""工具执行管线门面：单次工具调用的唯一执行入口。

本模块承载 ``ToolExecutor``——模型请求的工具调用从「准入」到「归一化观察」的完整
管线编排：

    ToolAccessGate（注册表命中 / 权限门禁 / 参数校验 / PreToolUse Hook）
        → FileToolStateCoordinator（prepare / lock / check_stale / complete）
        → ToolHandlerRunner（thread 直跑 或 process 隔离 + 硬超时强杀）
        → PostToolUse Hook
        → ToolObservationBudget（模型通道脱敏截断落盘）

管线只做编排：每个阶段的实现各自收口在对应协作者中，本模块不含隔离执行细节、
权限文案或预算算法。上层（``WorkflowOperations``）只依赖 ``execute`` 一个入口，
拿到的始终是已治理、可落库、可回传的 :class:`ToolObservation`。
"""

from collections.abc import Collection

from app.core.tools.guard.file_resource_paths import FileResourcePathError
from app.core.tools.guard.file_tool_state_coordinator import (
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
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_handler_runner import ToolHandlerRunner
from app.core.tools.tool_execute.tool_observation_budget import ToolObservationBudget
from app.core.tools.tool_handler.terminal import OutputSink
from app.core.tools.tool_registry import ToolRegistry
from app.hook import HookContext
from app.hook.hook_event import HookEvent
from app.hook.hook_interceptor import HookInterceptor


class ToolExecutor:
    """模型请求工具调用的统一执行入口（执行管线门面）。

    单一职责：把「模型请求的一次工具调用」编排为「准入门禁 → 文件状态协调 →
    隔离执行 → PostToolUse Hook → 输出预算」五阶段管线，并始终返回归一化的
    :class:`ToolObservation`，使上层（workflow / 运行时）无需关心失败原因细节。

    职责边界：
    - 负责：按固定时序编排五个阶段，并保证**每条**返回路径（含门禁拒绝、状态协调
      提前返回、执行失败）都过同一道输出预算。
    - 不负责：门禁判定规则（``ToolAccessGate``）、文件 revision/stale/锁机制
      （``FileToolStateCoordinator``）、子进程隔离与超时强杀
      （``ToolHandlerRunner``）、预算算法（``ToolObservationBudget``）、
      handler 业务逻辑、模型可见性之外的运行策略。
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

    def execute(
        self,
        call: ToolCall,
        execution_context: ToolExecutionContext | None = None,
        allowed_tool_names: Collection[str] | None = None,
        output_sink: OutputSink | None = None,
    ) -> ToolObservation:
        """执行单次工具调用并返回归一化观察结果。

        编排顺序：准入门禁（注册表命中 → 权限门禁 → 参数校验 → PreToolUse Hook）
        → 文件工具经状态协调（revision/stale/锁）后委派隔离执行器，非文件工具由
        协调器短路为空计划直接执行 → PostToolUse Hook → 输出预算；
        任一前置环节失败都直接返回带 ``reason`` 的 :class:`ToolObservation`，
        绝不抛出，使上层始终拿到可落库/可回传的结果。

        参数:
            call: 模型请求的工具调用，含工具名、参数与调用 id。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                透传给执行器并由 handler 在执行期消费，便于后续扩展更多执行参数。
            allowed_tool_names: 当前 Agent profile 允许运行的工具名；为 None 表示
                调用方不增加 Agent 级门禁。
            should_cancel: 可选取消检查回调；透传给工具执行器，由 process 模式在等待
                结果时轮询、thread 模式在 handler 执行前后边界检查，命中即返回取消观察。
            output_sink: 可选实时输出回调；透传给执行器，使 process 工具（当前仅
                ``execute_terminal``）的运行期输出可增量回传上层做实时展示。
                **当前接线状态**：``should_cancel`` 已由 workflow 链路
                （``WorkflowOperations``）下穿，但 ``output_sink`` 尚未接入，
                终端实时流式展示仍未接通；目前仅直接调用方（单测 / 未来事件桥）注入。

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
            校验失败而短路返回，不进入执行阶段。
        """
        if execution_context is None:
            raise ValueError("execution_context is required")

        # 阶段一：准入门禁。任一环节拒绝即短路返回（已过预算）。
        gate_outcome = self._gate.evaluate(call, execution_context, allowed_tool_names)
        if not gate_outcome.admitted or gate_outcome.tool is None:
            denial = gate_outcome.denial or self._unknown_denial(call)
            return self._budget.apply(denial, execution_context)
        tool = gate_outcome.tool

        # 阶段二：文件状态协调的准备阶段。文件工具经 revision/stale/锁协调；非文件
        # 工具由协调器短路为空计划（lock/check_stale/complete 全 no-op），
        # 单一路径避免双分支重复 execute+hook 编排。
        try:
            plan = self._state_coordinator.prepare(
                tool,
                gate_outcome.arguments,
                execution_context,
                tool_call_id=call.call_id,
            )
        # FileResourcePathError 继承自 ValueError，必须在前面的 except 命中；若被
        # 调到下方宽泛 (OSError, RuntimeError, ValueError) 分支，将丢失面向模型的
        # 富文本 reason、退化为泛化文案。此顺序是显式契约，改动前须确认。
        except FileResourcePathError as exc:
            return self._budget.apply(
                tool_error(
                    tool.name,
                    str(exc),
                    reason=exc.reason,
                    permission=tool.permission,
                    tool_call_id=call.call_id,
                ),
                execution_context,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return self._budget.apply(
                tool_error(
                    tool.name,
                    f"invalid file path: {exc}",
                    reason=(
                        f"the file path is invalid: {exc}; this is deterministic, "
                        f"so pass a well-formed path inside the project. The same "
                        f"malformed path will always be rejected."
                    ),
                    permission=tool.permission,
                    tool_call_id=call.call_id,
                ),
                execution_context,
            )

        if plan.early_observation is not None:
            return self._budget.apply(plan.early_observation, execution_context)

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
                observation = self._runner.execute(
                    tool,
                    gate_outcome.arguments,
                    execution_context=execution_context,
                    tool_call_id=call.call_id,
                    output_sink=output_sink,
                )
                self._state_coordinator.complete(
                    plan,
                    observation,
                    execution_context,
                )
        except RuntimeError as exc:
            return self._budget.apply(
                tool_error(
                    tool.name,
                    str(exc),
                    reason=(
                        f"the file state coordinator could not process the "
                        f"request: {exc}; this is usually transient (e.g. a "
                        f"temporary capacity or lock limit), so retrying the same "
                        f"call may succeed once the condition clears."
                    ),
                    retryable=True,
                    permission=tool.permission,
                    tool_call_id=call.call_id,
                ),
                execution_context,
            )

        # 阶段四：PostToolUse 拦截点。门禁硬拒绝的路径不会走到这里（未执行、无观察）。
        HookInterceptor.safe_fire(
            HookContext.from_locatable(
                event=HookEvent.POST_TOOL_USE,
                locatable=execution_context,
                tool_name=tool.name,
                tool_observation=observation,
            )
        )

        # 阶段五：统一输出预算。
        return self._budget.apply(observation, execution_context)

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
