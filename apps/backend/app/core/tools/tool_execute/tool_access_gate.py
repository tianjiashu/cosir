"""工具调用准入门禁：解析工具定义 + 权限门禁 + 参数校验 + PreToolUse Hook。

这是工具执行管线（``ToolExecutor.execute``）的第一阶段：把「模型请求的一次工具调用」
翻译为「可以安全交给隔离执行器的工具定义 + 已校验参数」，或一条归一化的拒绝观察。
四段检查（注册表命中 / Agent profile 权限 / 参数 schema / PreToolUse Hook）同属
「准入决策」这一职责，故收口在本模块，不在管线门面里平铺。

职责边界：
- 负责：解析工具定义、按 ``allowed_tool_names`` 做 Agent 级门禁、参数校验、PreToolUse
  Hook 拦截（DENY 短路、modified_arguments 改写），并把四类拒绝各自的原因文案归一为
  面向模型的富文本观察。
- 不负责：文件状态协调、隔离执行与硬超时强杀、输出预算（以上归
  ``FileToolStateCoordinator`` / ``ToolHandlerRunner`` / ``ToolObservationBudget``）。
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from app.core.tools.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_registry import ToolRegistry
from app.core.tools.validation.arguments import (
    ToolArgumentValidation,
    validate_tool_arguments,
)
from app.hook import HookContext
from app.hook.hook_event import HookDecision, HookEvent
from app.hook.hook_interceptor import HookInterceptor
from app.hook.hook_result import HookResult


@dataclass(frozen=True)
class ToolGateOutcome:
    """一次工具调用准入门禁的判定结果。

    字段:
        tool: 通过全部检查的工具定义；被拒时为 ``None``，此时 ``denial`` 非空。
        arguments: 准入后的最终参数字典（已过 schema 校验，可能被 PreToolUse Hook
            改写）；被拒时为空字典。被 Hook 改写后的参数是执行器真正使用的入参，
            Hook 改写必须在执行前生效，故由本对象承载而非回写 ``ToolCall``。
        denial: 被拒时的归一化 error 观察；通过时为 ``None``。该观察**尚未**过输出
            预算，由 ``ToolExecutor`` 统一应用，保证所有返回路径预算口径一致。
    """

    tool: ToolDefinition | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    denial: ToolObservation | None = None

    @property
    def admitted(self) -> bool:
        """返回本次调用是否通过准入门禁。

        参数:
            无。

        返回:
            ``tool`` 非空且 ``denial`` 为空时返回 ``True``；否则 ``False``。

        异常:
            无。

        副作用:
            无（只读自身字段）。
        """

        return self.tool is not None and self.denial is None


class ToolAccessGate:
    """工具调用准入门禁：判定一次工具调用能否进入隔离执行阶段。"""

    def __init__(self, registry: ToolRegistry) -> None:
        """初始化准入门禁。

        参数:
            registry: 工具注册表，提供工具定义查询。

        返回:
            无。

        异常:
            无。

        副作用:
            持有 ``registry`` 引用，不触发任何工具执行。
        """

        self._registry = registry

    def evaluate(
        self,
        call: ToolCall,
        execution_context: ToolExecutionContext,
        allowed_tool_names: Collection[str] | None = None,
    ) -> ToolGateOutcome:
        """按顺序执行四段准入检查并返回判定结果。

        检查顺序：注册表命中 → Agent profile 权限门禁 → 参数校验 → PreToolUse Hook。
        任一环节拒绝即短路返回 ``denial``，不继续后续检查、不执行工具、不触发
        PostToolUse Hook。全部通过时返回 ``admitted=True`` 的判定结果。

        参数:
            call: 模型请求的工具调用，含工具名、参数与调用 id。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；由
                ``ToolExecutor`` 保证非 None，用于构造 Hook 上下文。
            allowed_tool_names: 当前 Agent profile 允许运行的工具名；为 None 表示
                调用方不增加 Agent 级门禁。

        返回:
            :class:`ToolGateOutcome`：通过时 ``admitted=True`` 且携带工具定义与最终
            参数；被拒时 ``denial`` 为经 :func:`tool_error` 构造的 error 观察，
            ``reason`` 为面向模型的富文本（根因 + 修正建议 + 与 ``retryable``
            一致的重试提示）。

        异常:
            无（四类拒绝全部归一化为 denial 观察，不向外抛出）。

        副作用:
            触发 PreToolUse Hook；Hook 自身异常时由 ``HookInterceptor.safe_fire``
            兜底放行（失败安全），不阻断工具执行。
        """

        tool = self._registry.get_tool_definition(call.tool_name)
        if tool is None:
            return ToolGateOutcome(
                denial=tool_error(
                    call.tool_name,
                    f"unknown tool: {call.tool_name}",
                    reason=(
                        f"the tool '{call.tool_name}' is not registered in the tool "
                        f"system; this is deterministic, so check the tool name for "
                        f"typos or register the tool before calling it again. The same "
                        f"name will always be rejected."
                    ),
                    tool_call_id=call.call_id,
                )
            )

        if allowed_tool_names is not None and tool.name not in allowed_tool_names:
            return ToolGateOutcome(
                denial=tool_error(
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
                )
            )

        validation = validate_tool_arguments(
            call.arguments,
            tool.parameters_schema,
            tool.args_model,
        )
        if not validation.ok:
            return ToolGateOutcome(
                denial=tool_error(
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
                )
            )

        arguments = self._apply_pre_tool_use_hook(
            tool, validation, execution_context, call.call_id
        )
        if isinstance(arguments, ToolObservation):
            return ToolGateOutcome(denial=arguments)
        return ToolGateOutcome(tool=tool, arguments=arguments)

    @staticmethod
    def _apply_pre_tool_use_hook(
        tool: ToolDefinition,
        validation: ToolArgumentValidation,
        execution_context: ToolExecutionContext,
        tool_call_id: str,
    ) -> dict[str, Any] | ToolObservation:
        """触发 PreToolUse 拦截点，返回最终参数或硬拒绝观察。

        Hook 在参数校验通过后、执行前触发。``DENY`` 是硬拒绝，短路返回 error 观察
        （不执行工具、不触发 PostToolUse Hook）；``modified_arguments`` 非空时替换
        最终参数后再继续执行。注册表未初始化或 Hook 抛异常时，
        ``HookInterceptor.safe_fire`` 返回 None，按放行处理（失败安全）。

        参数:
            tool: 已通过注册表与权限门禁的工具定义。
            validation: 已通过的参数校验结果。
            execution_context: 本次执行的运行时边界，用于构造 Hook 上下文。
            tool_call_id: 关联的模型工具调用 id，用于回写拒绝观察。

        返回:
            放行时为最终参数字典（可能被 Hook 改写）；硬拒绝时为 :class:`ToolObservation`。

        异常:
            无（Hook 异常由 ``safe_fire`` 内部兜底）。

        副作用:
            触发 PreToolUse Hook，可能产生副作用（Hook 自行负责）。
        """

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
            reason = decision.deny_reason or "blocked by pre-tool-use hook"
            return tool_error(
                tool.name,
                reason,
                reason=reason,
                permission=tool.permission,
                tool_call_id=tool_call_id,
            )
        if decision.modified_arguments is not None:
            return decision.modified_arguments
        return validation.arguments
