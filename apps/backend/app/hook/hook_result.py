"""Hook 执行结果值对象。

单一职责：承载「一次 Hook 执行后返回给触发方的决策」这一载体。触发方
（``ToolAccessGate`` / ``ToolExecutor`` 经由 ``app.hook.hook_interceptor.HookInterceptor``）按
``decision`` 决定放行 / 拒绝 / 改写参数。``additional_context`` 首版无消费方，
仅作未来通道占位（``Hook机制技术方案.md`` §6.3，D7）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.hook.hook_event import HookDecision


@dataclass(frozen=True)
class HookResult:
    """一次 Hook 执行的返回结果。

    属性:
        decision: 决策枚举（``HookDecision.ALLOW`` / ``HookDecision.DENY``）；缺省为
            ``ALLOW``（失败安全语义：Hook 异常或被跳过都不应阻断主流程）。
        deny_reason: 拒绝原因；``DENY`` 时必填（展示给用户），``ALLOW`` 时为 ``None``。
        modified_arguments: 改写后的工具入参；仅 ``PRE_TOOL_USE`` 的 ALLOW 可携带，
            其它事件或不改写时为 ``None``。
        additional_context: 附加上下文文本；首版无消费方（D7），仅作通道占位。
    """

    decision: HookDecision = HookDecision.ALLOW
    deny_reason: str | None = None
    modified_arguments: dict | None = None
    additional_context: str | None = None

    def __post_init__(self) -> None:
        """校验 DENY 时的必填项并归一化非法决策。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            当 ``decision`` 为 ``None``（外部构造误传）时按 ``HookDecision.ALLOW`` 兜底；
            ``DENY`` 时若 ``deny_reason`` 为空则补默认文案，保证下游总能拿到拒绝原因。
        """
        if self.decision is None:
            object.__setattr__(self, "decision", HookDecision.ALLOW)
        if self.decision == HookDecision.DENY and not self.deny_reason:
            object.__setattr__(self, "deny_reason", "blocked by hook")

    @classmethod
    def allow(cls) -> HookResult:
        """便捷构造默认放行结果。

        参数:
            无。

        返回:
            决策为 ``ALLOW`` 的 ``HookResult``。

        异常:
            无。

        副作用:
            无。
        """
        return cls(decision=HookDecision.ALLOW)
