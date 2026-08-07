"""工具调度器：对模型请求的工具调用做权限门禁 + 参数校验 + 隔离执行编排。"""

import dataclasses
from collections.abc import Callable, Collection

from app.hook import HookContext
from app.hook.hook_event import HookDecision, HookEvent
from app.hook.hook_interceptor import HookInterceptor
from app.hook.hook_result import HookResult
from app.tools.guard.display_data_budget import DisplayDataBudget
from app.tools.guard.file_resource_paths import FileResourcePathError
from app.tools.guard.file_tool_state_coordinator import (
    FileToolStateCoordinator,
)
from app.tools.guard.tool_output_budget import ToolOutputBudget
from app.tools.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.tools.tool_execute.tool_error import tool_error
from app.tools.tool_execute.tool_executor import ToolExecutor
from app.tools.tool_handler.terminal import OutputSink
from app.tools.tool_registry import ToolRegistry
from app.tools.validation.arguments import validate_tool_arguments


class ToolScheduler:
    """模型请求工具调用的统一调度入口。

    单一职责：对模型请求的工具调用做「权限策略校验 + 参数校验 + 隔离执行」
    的三段式编排，并始终返回归一化的 :class:`ToolObservation`，使上层
    （workflow / 运行时）无需关心失败原因细节。

    职责边界：
    - 负责：从注册表解析工具定义、按 ``allowed_permissions`` 做权限门禁、
      调用 ``validation`` 做参数校验、委派 :class:`ToolExecutor` 隔离执行。
    - 不负责：子进程隔离与超时强杀（``ToolExecutor``）、handler 业务逻辑、
      跨进程日志桥接、模型可见性之外的运行策略。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        state_coordinator: FileToolStateCoordinator | None = None,
        output_budget: ToolOutputBudget | None = None,
        display_data_budget: DisplayDataBudget | None = None,
    ) -> None:
        """初始化调度器并固化权限策略。

        参数:
            registry: 工具注册表，提供工具定义查询。
            state_coordinator: 文件 revision、重复调用和路径锁协作者。
            output_budget: 模型可见 ``content`` 的统一输出预算。
            display_data_budget: 客户端展示数据通道的统一字符预算。

        返回:
            无。

        异常:
            无。

        副作用:
            持有 ``registry``、``state_coordinator``、``executor`` 引用；不触发任何工具执行。
        """

        self._registry = registry
        self._executor = ToolExecutor()
        self._state_coordinator = state_coordinator or FileToolStateCoordinator()
        self._output_budget = output_budget or ToolOutputBudget()
        self._display_data_budget = display_data_budget or DisplayDataBudget()

    def list_tools(self) -> list[ToolDefinition]:
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

    def execute(
        self,
        call: ToolCall,
        execution_context: ToolExecutionContext | None = None,
        allowed_tool_names: Collection[str] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        output_sink: OutputSink | None = None,
    ) -> ToolObservation:
        """执行单次工具调用并返回归一化观察结果。

        编排顺序：注册表命中 → 权限门禁 → 参数校验 → 文件工具经状态协调
        （revision/stale/锁）后委派执行器隔离执行，非文件工具直接委派执行器
        隔离执行；
        任一前置环节失败都直接返回带 ``reason`` 的 :class:`ToolObservation`，
        绝不抛出，使上层始终拿到可落库/可回传的结果。

        参数:
            call: 模型请求的工具调用，含工具名、参数与调用 id。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                透传给执行器并由 handler 在执行期消费，便于后续扩展更多执行参数。
            allowed_tool_names: 当前 Agent profile 允许运行的工具名；为 None 表示
                调用方不增加 Agent 级门禁。
            should_cancel: 可选取消检查回调；透传给 process 工具执行器用于中止长工具。
            output_sink: 可选实时输出回调；透传给执行器，使 process 工具（当前仅
                ``execute_terminal``）的运行期输出可增量回传上层做实时展示。

        返回:
            归一化后的 :class:`ToolObservation`：成功为 status="success"；
            未知工具 / 权限拒绝 / 参数非法 / 执行失败为 status="error"，
            每种失败均经 :func:`tool_error` 构造，``reason`` 为面向模型的
            富文本（根因 + 修正建议 + 与 ``retryable`` 一致的重试提示），
            而非稳定机器短码。

        异常:
            无（所有失败路径均归一化为 error 观察）。

        副作用:
            委派 :class:`ToolExecutor` 启动子进程执行；可能因权限或参数
            校验失败而短路返回，不进入执行阶段。
        """
        if execution_context is None:
            raise ValueError("execution_context is required")

        # 检查工具是否存在
        tool = self._registry.get_tool_definition(call.tool_name)
        if tool is None:
            return self._apply_output_budget(
                tool_error(
                    call.tool_name,
                    f"unknown tool: {call.tool_name}",
                    reason=(
                        f"the tool '{call.tool_name}' is not registered in the tool "
                        f"system; this is deterministic, so check the tool name for "
                        f"typos or register the tool before calling it again. The same "
                        f"name will always be rejected."
                    ),
                    tool_call_id=call.call_id,
                ),
                execution_context,
            )

        # 检查工具是否在允许的工具名列表中
        if allowed_tool_names is not None and tool.name not in allowed_tool_names:
            return self._apply_output_budget(
                tool_error(
                    tool.name,
                    f"agent profile denied tool: {tool.name},allow tool names:{allowed_tool_names}",
                    reason=(
                        f"the current agent profile does not allow calling "
                        f"'{tool.name}'; the allowed tools are "
                        f"{sorted(allowed_tool_names)}. This is deterministic under "
                        f"the current profile, so choose an allowed tool or update "
                        f"the profile's allowed_permissions before retrying."
                    ),
                    permission=tool.permission,
                    tool_call_id=call.call_id,
                ),
                execution_context,
            )

        # 检查工具参数是否合法
        validation = validate_tool_arguments(
            call.arguments,
            tool.parameters_schema,
            tool.args_model,
        )

        # 如果参数不合法，则返回错误
        if not validation.ok:
            return self._apply_output_budget(
                tool_error(
                    tool.name,
                    f"invalid tool arguments: {validation.error}",
                    reason=(
                        f"the tool arguments failed validation: {validation.error}; "
                        f"this is deterministic, so fix the argument values/types per "
                        f"the tool's parameter schema and call again. The same "
                        f"arguments will always be rejected."
                    ),
                    permission=tool.permission,
                    tool_call_id=call.call_id,
                ),
                execution_context,
            )
        # PreToolUse 拦截点（Hook 机制）：参数校验通过后、执行前。
        # 直接调用 HookInterceptor.before_tool_call 静态方法。硬拒绝时短路返回
        # tool_error 且不执行工具、不触发 after_tool_call；改写参数时替换
        # validation.arguments 后再继续。注册表未初始化 / Hook 异常时 HookInterceptor
        # 兜底放行（失败安全）。
        decision = (
            HookInterceptor.safe_fire(
                HookContext.from_locatable(
                    event=HookEvent.PRE_TOOL_USE,
                    locatable=execution_context,
                    tool_name=tool.name,
                    tool_arguments=validation.arguments,
                )
            )
            or HookResult.allow()
        )
        if decision.decision == HookDecision.DENY:
            return self._apply_output_budget(
                tool_error(
                    tool.name,
                    decision.deny_reason or "blocked by pre-tool-use hook",
                    reason=decision.deny_reason,
                    permission=tool.permission,
                    tool_call_id=call.call_id,
                ),
                execution_context,
            )
        if decision.modified_arguments is not None:
            validation = dataclasses.replace(validation, arguments=decision.modified_arguments)

        # 统一执行管线：文件工具经状态协调（revision/stale/锁）；非文件工具由
        # 协调器短路为空计划直接执行（lock/check_stale/complete 全 no-op）。
        # 单一路径避免双分支重复 execute+hook 编排。
        try:
            plan = self._state_coordinator.prepare(
                tool,
                validation.arguments,
                execution_context,
                tool_call_id=call.call_id,
            )
        # FileResourcePathError 继承自 ValueError，必须在前面的 except 命中；若被
        # 调到下方宽泛 (OSError, RuntimeError, ValueError) 分支，将丢失面向模型的
        # 富文本 reason、退化为泛化文案。此顺序是显式契约，改动前须确认。
        except FileResourcePathError as exc:
            return self._apply_output_budget(
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
            return self._apply_output_budget(
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
            return self._apply_output_budget(plan.early_observation, execution_context)

        try:
            with self._state_coordinator.lock(plan, execution_context):
                stale_observation = self._state_coordinator.check_stale(
                    plan,
                    tool,
                    execution_context,
                    tool_call_id=call.call_id,
                )
                if stale_observation is not None:
                    return self._apply_output_budget(stale_observation, execution_context)
                observation = self._executor.execute(
                    tool,
                    validation.arguments,
                    execution_context=execution_context,
                    tool_call_id=call.call_id,
                    should_cancel=should_cancel,
                    output_sink=output_sink,
                )
                self._state_coordinator.complete(
                    plan,
                    observation,
                    execution_context,
                )
        except RuntimeError as exc:
            return self._apply_output_budget(
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

        HookInterceptor.safe_fire(
            HookContext.from_locatable(
                event=HookEvent.POST_TOOL_USE,
                locatable=execution_context,
                tool_name=tool.name,
                tool_observation=observation,
            )
        )
        return self._apply_output_budget(observation, execution_context)

    def _apply_output_budget(
        self,
        observation: ToolObservation,
        execution_context: ToolExecutionContext | None,
    ) -> ToolObservation:
        """对任意成功、失败或提前返回观察统一应用输出预算，超出预算截断，并保留本地文件。

        模型通道（``content``）与客户端展示通道（``data``）分别受
        :class:`ToolOutputBudget` 与 :class:`DisplayDataBudget` 约束，避免展示
        通道绕过模型通道预算无约束膨胀。

        参数:
            observation: 待返回给上层的工具观察。
            execution_context: 当前 workspace 上下文。

        返回:
            已脱敏并受两条通道字符预算约束的观察。

        异常:
            无。artifact 写入失败由 :class:`ToolOutputBudget` 内部退化处理。

        副作用:
            超限且有 workspace 时可能写入脱敏 artifact。
        """

        budgeted = self._output_budget.apply(observation, execution_context)
        return self._display_data_budget.apply(budgeted)
