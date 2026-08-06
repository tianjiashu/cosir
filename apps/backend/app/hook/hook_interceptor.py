"""所有 Hook 拦截的统一收口（``HookInterceptor`` 静态方法）。

单一职责：把分散在运行期各处的「拦截点」——工具调用（Pre/PostToolUse）、
运行期事件（UserPromptSubmit/Stop/PreCompact）、会话事件（SessionStart/SessionEnd）——
全部翻译成对 ``HookRegistry.fire`` 的调用，是 Hook 机制对外唯一的拦截收口点。

本模块全部以静态方法对外，调用方（``ToolScheduler`` / ``runner`` / ``app`` 装配层）
直接调用，无需注入实例。原 ``ToolCallInterceptor`` 协议已删除；非工具类的散落
触发逻辑（``runner._fire_hook`` / ``app._fire_session_hook``）也已收敛于此。

设计要点：
- 工具拦截有决策返回值（``HookResult``），deny 会硬阻断工具执行；
- 运行期/会话事件为「接通即放行」切面，当前无内置消费方，返回值为 ``None``，
  异常一律兜底放行（失败安全），绝不阻断主流程。
- 运行期事件在异步协程内触发时，``fire`` 是同步阻塞调用，故 ``fire_run_event``
  以 ``asyncio.to_thread`` 调度到 worker 线程，避免卡死事件循环。
- 所有触发共享 ``_safe_fire`` 兜底模板（构造上下文 + fire + 异常兜底 + 日志），
  不再各方法重复 try/except。
"""

from __future__ import annotations

import asyncio

from app.config.logging.logger import log
from app.hook.hook_context import HookContext
from app.hook.hook_event import HookEvent
from app.hook.hook_registry import get_hook_registry
from app.hook.hook_result import HookResult
from app.tools.schemas.tool_definition import ToolDefinition
from app.tools.schemas.tool_execution_context import ToolExecutionContext
from app.tools.schemas.tool_observation import ToolObservation


class HookInterceptor:
    """所有 Hook 拦截的收口实现（静态方法）。

    三类时机：
    - ``before_tool_call`` / ``after_tool_call`` → ``PRE_TOOL_USE`` / ``POST_TOOL_USE``；
      前者 ``DENY`` 决策硬拒绝、``modified_arguments`` 改写；后者为审计切面（拒绝不阻断）。
    - ``fire_run_event`` → 运行期事件（UserPromptSubmit/Stop/PreCompact），异步切面。
    - ``fire_session_event`` → 会话事件（SessionStart/SessionEnd），同步切面。

    所有时机的 ``fire`` 统一经 ``_safe_fire`` 兜底，失败安全（异常兜底放行 + 日志）。
    """

    # ------------------------------------------------------------------ #
    # 触发的统一兜底模板
    # ------------------------------------------------------------------ #

    @staticmethod
    def _safe_fire(context: HookContext, log_key: str, msg: str) -> HookResult | None:
        """以失败安全语义触发一次 Hook，统一兜底异常。

        供各拦截时机复用，消除重复的 try/except。返回 ``HookRegistry.fire`` 的
        ``HookResult``；注册表未初始化或 Hook 异常时返回 ``None``（调用方按放行处理）。

        参数:
            context: 待触发的 ``HookContext``。
            log_key: 异常日志的 msg 模板键（如 ``hook_pre_tool_use_failed``）。
            msg: 异常日志的可读原因（中文），用于定位。

        返回:
            正常触发返回 ``HookResult``；异常或注册表未初始化返回 ``None``。

        异常:
            无（内部异常吞掉并记 ``error`` 日志）。

        副作用:
            触发 ``HookRegistry.fire``；异常时写 error 日志。
        """
        try:
            return get_hook_registry().fire(context)
        except Exception:
            log.exception(log_key, extra={"msg": msg, "data": {"event": context.event.value}})
            return None

    # ------------------------------------------------------------------ #
    # 工具拦截（有决策返回值）
    # ------------------------------------------------------------------ #

    @staticmethod
    def before_tool_call(
        tool: ToolDefinition, context: ToolExecutionContext, arguments: dict
    ) -> HookResult:
        """触发 ``PRE_TOOL_USE`` Hook，返回决策结果。

        ``DENY`` 决策硬拒绝（调用方据此短路工具执行）；``ALLOW`` 携带 ``modified_arguments``
        时调用方应替换入参。注册表未初始化 / Hook 异常时兜底放行（失败安全）。

        参数:
            tool: 待执行工具定义。
            context: 工具执行上下文（取 workspace_id / task_id / turn_id）。
            arguments: 校验后的工具入参 dict。

        返回:
            ``HookResult``：``decision`` 为 ``DENY`` 时硬拒绝；``ALLOW`` 且带
            ``modified_arguments`` 时放行并携带改写参数；异常兜底为放行。

        异常:
            无（内部异常由 ``_safe_fire`` 兜底）。

        副作用:
            触发 ``HookRegistry.fire``（可能写审计/拒绝日志）；不修改 ``context``。
        """
        result = HookInterceptor._safe_fire(
            HookContext.from_locatable(
                event=HookEvent.PRE_TOOL_USE,
                locatable=context,
                tool_name=tool.name,
                tool_arguments=arguments,
            ),
            log_key="hook_pre_tool_use_failed",
            msg="PreToolUse 触发失败，兜底放行（不阻断工具执行）",
        )
        return result or HookResult.allow()

    @staticmethod
    def after_tool_call(
        tool: ToolDefinition,
        context: ToolExecutionContext,
        observation: object,
    ) -> HookResult:
        """触发 ``POST_TOOL_USE`` Hook（审计切面），返回决策结果。

        当前 after 决策的拒绝不阻断主流程；注册表未初始化 / Hook 异常时兜底放行。

        参数:
            tool: 已执行工具定义。
            context: 工具执行上下文。
            observation: 工具执行的 ``ToolObservation``。

        返回:
            恒为放行决策的 ``HookResult``（异常兜底为放行）。

        异常:
            无（内部异常由 ``_safe_fire`` 兜底）。

        副作用:
            触发 ``HookRegistry.fire``（``ToolAuditHook`` 记审计日志）；不修改 ``context``。
        """
        result = HookInterceptor._safe_fire(
            HookContext.from_locatable(
                event=HookEvent.POST_TOOL_USE,
                locatable=context,
                tool_name=tool.name,
                tool_observation=observation if isinstance(observation, ToolObservation) else None,
            ),
            log_key="hook_post_tool_use_failed",
            msg="PostToolUse 触发异常，已忽略（不阻断主流程）",
        )
        return result or HookResult.allow()

    # ------------------------------------------------------------------ #
    # 运行期事件（异步切面，接通即放行）
    # ------------------------------------------------------------------ #

    @staticmethod
    async def fire_run_event(event: HookEvent, task: object, turn: object) -> None:
        """触发一次运行期 Hook 事件（UserPromptSubmit/Stop/PreCompact 等）。

        在异步协程内触发时，``HookRegistry.fire`` 是同步阻塞调用，故以 ``asyncio.to_thread``
        调度到 worker 线程，避免卡死事件循环。异常由 ``_safe_fire`` 统一兜底为放行，
        绝不阻断主流程（失败安全语义）。

        参数:
            event: 待触发的 ``HookEvent`` 枚举（运行期事件）。
            task: 当前任务记录（取 ``task_id`` / ``workspace_id``）。
            turn: 当前轮次记录（取 ``turn_id``）。

        返回:
            无。

        异常:
            无（内部异常吞掉并记日志）。

        副作用:
            可能写日志（内置 Hook 或错误日志）；不修改运行时状态。
        """
        context = HookContext.from_locatable(event=event, locatable=task, turn=turn)
        await asyncio.to_thread(
            HookInterceptor._safe_fire,
            context,
            "hook_run_fire_failed",
            "run_turn 内 Hook 触发失败",
        )

    # ------------------------------------------------------------------ #
    # 会话事件（同步切面，接通即放行）
    # ------------------------------------------------------------------ #

    @staticmethod
    def fire_session_event(event_name: str) -> None:
        """触发一次会话事件 Hook（SessionStart/SessionEnd）。

        首版无内置实现，空订阅列表下 ``fire`` 零开销放行。触发异常由 ``_safe_fire`` 兜底，
        绝不阻断启动/关闭。

        参数:
            event_name: ``"SessionStart"`` 或 ``"SessionEnd"``（对应 ``HookEvent`` 值）。

        返回:
            无。

        异常:
            无（内部异常吞掉并记日志）。

        副作用:
            可能写日志（内置 Hook 或错误日志）；不修改运行时状态。
        """
        try:
            event = HookEvent(event_name)
        except ValueError:
            log.error(
                "hook_session_unknown_event",
                extra={"msg": f"未知会话事件，跳过: {event_name}", "data": {"event": event_name}},
            )
            return
        HookInterceptor._safe_fire(
            HookContext(event=event),
            log_key="hook_session_fire_failed",
            msg=f"Hook 会话事件触发失败: {event_name}",
        )
