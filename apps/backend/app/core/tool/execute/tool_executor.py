"""工具执行管线门面：单次工具调用的唯一执行入口。

本模块承载 ``ToolExecutor``——模型请求的工具调用从「准入」到「归一化观察」的完整
管线编排：

    ToolAccessGate（注册表查询 / Agent 工具白名单 / 参数解码与校验 / PreToolUse Hook）
        → running 投影（经 workflow ``emit``）
        → ToolHandlerRunner（thread 直跑 或 process 隔离 + 硬超时强杀）
        → PostToolUse Hook
        → ToolObservationBudget（模型通道预算与超限落盘）

管线只做编排：每个阶段的实现各自收口在对应协作者中，本模块不含隔离执行细节、
门禁规则或预算算法，也**不投影 Transport 状态**——running 与终态都由调用方
（``run_tool_batch``）经 workflow 的 ``emit`` 发出，tools 因此不依赖 Transport。
门禁拒绝和执行器返回的观察都会经过模型输出预算；只有真正交给 ``ToolHandlerRunner``
的路径会投影 running 并触发 PostToolUse Hook。
"""

from collections.abc import Collection

from app.core.events import ToolCallStatusChangedEvent
from app.core.hook import HookContext, HookEvent, HookInterceptor
from app.core.tool.schemas import (
    ToolCall,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.execute.tool_access_gate import ToolAccessGate, ToolGateOutcome
from app.core.tool.execute.tool_handler_runner import ToolHandlerRunner
from app.core.tool.execute.tool_observation_budget import ToolObservationBudget
from app.core.tool.tool_registry import ToolRegistry
from app.core.workflow.node_helper import emit


class ToolExecutor:
    """模型请求工具调用的统一执行入口（执行管线门面）。

    职责边界：
    - 负责：按固定时序编排准入门禁、handler 隔离执行、PostToolUse Hook 和输出预算，正常
      返回时给出归一化的 :class:`ToolObservation`。
    - 不负责：门禁判定规则（``ToolAccessGate``）、子进程隔离与超时强杀（``ToolHandlerRunner``）、
      预算算法（``ToolObservationBudget``）、Transport 状态投影（调用方经 ``emit``）、
      handler 业务逻辑。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._gate = ToolAccessGate(registry)
        self._runner = ToolHandlerRunner()
        self._budget = ToolObservationBudget()

    def execute(
        self,
        call: ToolCall,
        execution_context: ToolExecutionContext,
        allowed_tool_names: Collection[str],
    ) -> ToolObservation:
        """执行单次工具调用并返回归一化观察。

        编排顺序：准入门禁 → running 投影 → handler 隔离执行 → PostToolUse Hook → 输出预算。
        门禁拒绝只经过输出预算，不投影 running、不触发 PostToolUse Hook。

        参数:
            call: 模型请求的工具调用。
            execution_context: 本次调用的完整运行时边界，由调用方按 ``call`` 构造（含
                ``tool_call_id``）；本方法直接信任，不再特化。
            allowed_tool_names: 本轮准入的工具名；不在其中的已注册工具被拒绝。

        返回:
            成功为 ``status="success"``；拒绝或执行失败为 ``error``；取消为 ``cancelled``。所有返回路径的 ``content`` 均经过输出预算，``display_data`` 不截断。

        异常:
            未归一化异常原样上抛，由调用方收口为错误观察。

        副作用:
            按 ``execution_mode`` 在当前线程或隔离子进程执行 handler；handler 自身可能产生外部
            副作用。
        """

        gate_outcome:ToolGateOutcome = self._gate.evaluate(call, execution_context, allowed_tool_names)
        if gate_outcome.denial is not None:
            return self._budget.apply(gate_outcome.denial, execution_context)
        tool = gate_outcome.tool

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
        observation = self._runner.execute(tool, gate_outcome.arguments, execution_context)

        HookInterceptor.safe_fire(
            HookContext.from_locatable(
                event=HookEvent.POST_TOOL_USE,
                locatable=execution_context,
                tool_name=tool.name,
                tool_observation=observation,
            )
        )
        return self._budget.apply(observation, execution_context)
