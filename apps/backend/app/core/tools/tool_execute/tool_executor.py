"""工具执行管线门面：单次工具调用的唯一执行入口。

本模块承载 ``ToolExecutor``——模型请求的工具调用从「准入」到「归一化观察」的完整
管线编排：

    ToolAccessGate（注册表查询 / Agent 工具白名单 / 参数解码与校验 / PreToolUse Hook）
        → FileToolStateCoordinator（prepare / lock / check_stale / complete）
        → ``on_start`` 回调（调用方据此投影 running）
        → ToolHandlerRunner（thread 直跑 或 process 隔离 + 硬超时强杀）
        → PostToolUse Hook
        → ToolObservationBudget（模型通道预算与超限落盘）

管线只做编排：每个阶段的实现各自收口在对应协作者中，本模块不含隔离执行细节、
门禁规则或预算算法，也**不投影 Transport 状态**——running 与终态都由调用方
（``run_tool_batch``）经 workflow 的 ``emit`` 发出，core/tools 因此不依赖 Transport。
门禁拒绝、状态协调提前返回和执行器返回的观察都会经过模型输出预算；只有真正交给
``ToolHandlerRunner`` 的路径会触发 ``on_start`` 与 PostToolUse Hook。
"""

from collections.abc import Collection

from app.core.events import ToolCallStatusChangedEvent
from app.core.hook import HookContext, HookEvent, HookInterceptor
from app.core.tools.guard.file_resource_paths import FileResourcePathError
from app.core.tools.guard.file_tool_state_coordinator import (
    FileToolExecutionPlan,
    FileToolStateCoordinator,
)
from app.core.tools.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_access_gate import ToolAccessGate, ToolGateOutcome
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_handler_runner import ToolHandlerRunner
from app.core.tools.tool_execute.tool_observation_budget import ToolObservationBudget
from app.core.tools.tool_registry import ToolRegistry
from app.core.workflow.node_helper import emit


class ToolExecutor:
    """模型请求工具调用的统一执行入口（执行管线门面）。

    职责边界：
    - 负责：按固定时序编排准入门禁、文件状态协调、handler 隔离执行、PostToolUse Hook
      和输出预算，正常返回时给出归一化的 :class:`ToolObservation`。
    - 不负责：门禁判定规则（``ToolAccessGate``）、文件 revision/stale/锁机制
      （``FileToolStateCoordinator``）、子进程隔离与超时强杀（``ToolHandlerRunner``）、
      预算算法（``ToolObservationBudget``）、Transport 状态投影（调用方经 ``emit``）、
      handler 业务逻辑。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._gate = ToolAccessGate(registry)
        self._state_coordinator = FileToolStateCoordinator()
        self._runner = ToolHandlerRunner()
        self._budget = ToolObservationBudget()

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
    ) -> FileToolExecutionPlan | ToolObservation:
        """准备文件状态计划；准备阶段的提前返回观察已应用输出预算。

        非文件工具也会得到空计划，锁、stale 检查与状态刷新对它们都是空操作，主流程因此不分叉。
        文件资源路径错误与 ``OSError`` / ``RuntimeError`` / ``ValueError`` 在此转成观察；状态
        协调器的其他异常向上传播。
        """

        try:
            plan = self._state_coordinator.prepare(
                tool,
                arguments,
                execution_context,
                tool_call_id=tool_call_id,
            )
        except FileResourcePathError as exc:
            return self._budget.apply(
                tool_error(
                    tool.name,
                    str(exc),
                    reason=exc.reason,
                    tool_call_id=tool_call_id,
                ),
                execution_context,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return self._budget.apply(
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
            return self._budget.apply(plan.early_observation, execution_context)
        return plan

    def _state_coordinator_error(
        self,
        tool: ToolDefinition,
        exc: RuntimeError,
        execution_context: ToolExecutionContext,
        tool_call_id: str,
    ) -> ToolObservation:
        """把状态协调器加锁 / 检查 / 完成阶段的运行期错误转换为可重试的失败观察。"""

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

    def execute(
        self,
        call: ToolCall,
        execution_context: ToolExecutionContext,
        allowed_tool_names: Collection[str],
    ) -> ToolObservation:
        """执行单次工具调用并返回归一化观察。

        编排顺序：准入门禁 → 文件状态准备 → 持锁并检查 stale → ``on_start`` → handler 隔离
        执行 → 成功时刷新文件状态 → PostToolUse Hook → 输出预算。门禁拒绝、准备阶段提前返回
        和 stale 观察只经过输出预算，不触发 ``on_start`` 与 PostToolUse Hook。

        参数:
            call: 模型请求的工具调用。
            execution_context: 本次调用的完整运行时边界，由调用方按 ``call`` 构造（含
                ``tool_call_id`` / ``user_decision``）；本方法直接信任，不再特化。
            allowed_tool_names: 本轮准入的工具名；不在其中的已注册工具被拒绝。
            on_start: 确认可以运行、即将交给 handler 时调用一次；调用方据此投影 running，
                因此排队中或被拒绝的调用不会先显示为运行中。它在文件路径锁内执行，只应做
                不阻塞的投影。

        返回:
            成功为 ``status="success"``；拒绝、路径错误、stale 或执行失败为 ``error``；取消为
            ``cancelled``。所有返回路径的 ``content`` 均经过输出预算，``display_data`` 不截断。

        异常:
            状态协调器准备阶段之外的未归一化异常原样上抛，由调用方收口为错误观察。

        副作用:
            按 ``execution_mode`` 在当前线程或隔离子进程执行 handler；文件工具会读取文件状态、
            获取进程内路径锁并更新 revision / 重复调用状态；handler 自身可能产生外部副作用。
        """

        gate_outcome:ToolGateOutcome = self._gate.evaluate(call, execution_context, allowed_tool_names)
        if gate_outcome.denial is not None:
            return self._budget.apply(gate_outcome.denial, execution_context)
        tool = gate_outcome.tool

        plan = self._prepare_state(tool, gate_outcome.arguments, execution_context, call.call_id)
        if isinstance(plan, ToolObservation):
            return plan

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
                emit(
                    ToolCallStatusChangedEvent(
                        task_id=execution_context.task_id,
                        run_id=execution_context.run_id,
                        step_id=execution_context.step_id,
                        tool_call_id=execution_context.tool_call_id,
                        status="running",
                        # 已过准入：展示的就是交给 handler 的最终参数（含 PreToolUse Hook 改写）。
                        args=gate_outcome.arguments,
                    )
                )
                observation = self._runner.execute(
                    tool, gate_outcome.arguments, execution_context
                )
                self._state_coordinator.complete(plan, observation, execution_context)
        except RuntimeError as exc:
            return self._state_coordinator_error(tool, exc, execution_context, call.call_id)

        HookInterceptor.safe_fire(
            HookContext.from_locatable(
                event=HookEvent.POST_TOOL_USE,
                locatable=execution_context,
                tool_name=tool.name,
                tool_observation=observation,
            )
        )
        return self._budget.apply(observation, execution_context)
