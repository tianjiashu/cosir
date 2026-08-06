"""内置 Hook：工具执行审计。

单一职责：在 ``POST_TOOL_USE`` 时机记录工具调用结果，作为统一的审计落点。
它与既有工具执行日志是**互补**而非重复——既有日志分布在各 handler / service 内部、
归属「执行链路」，本 Hook 归属「Hook 机制对工具调用的统一观察切面」，可用于未来
扩展（如统计、二次处理），是 Hook 机制的最小可用性验证（``Hook机制技术方案.md`` §7）。

不记录 secret / 敏感个人信息：只记工具名、执行成功标志、输出长度、task/turn 等
定位信息，不记工具入参原文（可能含路径以外的敏感内容），也不记完整输出。
"""

from __future__ import annotations

from app.config.logging.logger import log
from app.hook.hook_base import HookBase
from app.hook.hook_context import HookContext
from app.hook.hook_event import HookEvent
from app.hook.hook_result import HookResult


class ToolAuditHook(HookBase):
    """工具执行后审计 Hook（``POST_TOOL_USE``，空 matcher 即所有工具）。"""

    def __init__(self) -> None:
        """初始化审计 Hook，绑定 ``POST_TOOL_USE`` 事件、空 matcher。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            调用基类构造，固化 ``event=POST_TOOL_USE``、``matcher=None``。
        """
        super().__init__(event=HookEvent.POST_TOOL_USE, matcher=None)

    @property
    def name(self) -> str:
        """稳定标识。

        参数:
            无。

        返回:
            固定字符串 ``"tool_audit"``（与既有的 ``tool_executed`` 日志事件区分，
            避免混淆）。
        """
        return "tool_audit"

    def execute(self, context: HookContext) -> HookResult:
        """记录工具执行审计日志并返回放行。

        参数:
            context: ``POST_TOOL_USE`` 触发上下文，取 ``tool_name`` / ``tool_observation`` /
                ``task_id`` / ``turn_id`` 等定位信息。

        返回:
            恒为 ``HookResult.allow()``（审计 Hook 不拦截）。

        异常:
            无（内部异常由 ``HookRegistry.fire`` 兜底为 ALLOW）。

        副作用:
            写 debug 级审计日志（不含 secret / 完整输出 / 入参原文），不污染默认
            info 输出（对齐 ``Hook机制技术方案.md`` §7.2）。
        """
        obs = context.tool_observation
        success = bool(obs and obs.status == "success")
        output_len = 0
        if obs is not None and isinstance(obs.content, str):
            output_len = len(obs.content)
        log.debug(
            "tool_audit",
            extra={
                "msg": "工具执行审计",
                "data": {
                    "tool_name": context.tool_name,
                    "success": success,
                    "output_len": output_len,
                    "task_id": context.task_id,
                    "turn_id": context.turn_id,
                },
            },
        )
        return HookResult.allow()
