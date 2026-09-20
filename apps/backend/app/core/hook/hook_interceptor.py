"""所有 Hook 拦截的统一收口（``HookInterceptor`` 静态方法）。"""

from __future__ import annotations

import asyncio
import json

from app.config.logging.logger import log
from app.core.hook.hook_context import HookContext
from app.core.hook.hook_event import HookDecision
from app.core.hook.hook_registry import HookRegistry, get_hook_registry
from app.core.hook.hook_result import HookResult


class HookInterceptor:
    """所有 Hook 拦截的收口实现（静态方法），并承载执行编排（``fire``）。

    四类职责：
    - ``fire`` → 按事件解析注册表并聚合执行（matches 过滤 / DENY 短路 /
      modified_arguments 合并 / additional_context 拼接），失败安全兜底 ALLOW。
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
    def _fire(context: HookContext, registry: HookRegistry | None = None) -> HookResult:
        """触发某事件下所有 Hook，按失败安全语义聚合结果。

        编排规则（自 ``HookRegistry`` 迁移而来）：
        - 取 ``context.event`` 下全部 Hook（默认从全局注册表 ``get_hook_registry``
          解析，也可显式传入 ``registry`` 以支持隔离测试）；
        - 依次 ``matches(context)`` 过滤，未命中跳过；
        - 命中者 ``execute(context)``；任一抛异常 → 兜底 ``ALLOW`` 并记 error；
        - 首个 ``DENY`` 立即短路返回（后续 Hook 不再执行）；
        - ``PRE_TOOL_USE`` 下首个 ``modified_arguments`` 生效（覆盖式，后者覆盖前者）；
        - 空订阅列表 → 直接返回 ``ALLOW``（零开销）。

        参数:
            context: 触发上下文（只读，Hook 不得修改）。
            registry: 待解析 Hook 的注册表；缺省用进程级单例 ``get_hook_registry()``。

        返回:
            聚合后的 ``HookResult``：``decision`` 为首个 DENY 或最终 ALLOW；
            ``modified_arguments`` 为命中 Hook 中最后一个非空的改写值；
            ``additional_context`` 为所有命中 Hook 的非空 ``additional_context`` 拼接
            （换行分隔，首版无消费方，仅作通道占位，见 ``Hook机制技术方案.md`` §6.3）。

        异常:
            RuntimeError: ``registry`` 缺省且注册表未初始化（由 ``get_hook_registry``
                抛出，fail-fast，调用方经 ``_safe_fire`` 兜底放行）。

        副作用:
            可能写 error / warning 日志（Hook 异常或 DENY）；不修改 ``context``。
        """
        # 解析 Hook
        source = registry if registry is not None else get_hook_registry()
        hooks = source.resolve_for(context.event)
        if not hooks:
            return HookResult.allow()

        merged_args: dict | None = None
        context_parts: list[str] = []

        for hook in hooks:
            if not hook.matches(context):
                continue
            try:
                result = hook.execute(context)
            except Exception:
                log.exception(
                    "hook_execute_failed",
                    extra={
                        "msg": "Hook 执行异常，按 ALLOW 兜底放行",
                        "data": {
                            "hook_name": hook.name,
                            "event": context.event.value,
                            "tool_name": context.tool_name,
                        },
                    },
                )
                continue

            if result is None or not isinstance(result, HookResult):
                log.error(
                    "hook_invalid_result",
                    extra={
                        "msg": "Hook 返回非 HookResult，按 ALLOW 兜底放行",
                        "data": {
                            "hook_name": hook.name,
                            "event": context.event.value,
                        },
                    },
                )
                continue

            if result.decision == HookDecision.DENY:
                log.warning(
                    "hook_denied",
                    extra={
                        "msg": "Hook 拒绝执行",
                        "data": {
                            "hook_name": hook.name,
                            "event": context.event.value,
                            "tool_name": context.tool_name,
                            "reason": result.deny_reason,
                        },
                    },
                )
                return result

            if result.modified_arguments is not None:
                merged_args = result.modified_arguments
            if result.additional_context:
                context_parts.append(result.additional_context)

        return HookResult(
            decision=HookDecision.ALLOW,
            modified_arguments=merged_args,
            additional_context="\n".join(context_parts) if context_parts else None,
        )

    @staticmethod
    def safe_fire(context: HookContext) -> HookResult | None:
        """以失败安全语义触发一次 Hook，统一兜底异常。

        供各拦截时机复用，消除重复的 try/except。返回 ``HookInterceptor.fire`` 的
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
            触发 ``HookInterceptor.fire``；异常时写 error 日志。
        """
        try:
            return HookInterceptor._fire(context)
        except Exception:
            log.exception(
                context.event.value,
                extra={
                    "msg": "safe_fire_触发失败",
                    "data": {"event": json.dumps(context, default=str, ensure_ascii=False)},
                },
            )
            return None

    @staticmethod
    async def async_safe_fire(context: HookContext) -> HookResult | None:
        try:
            return await asyncio.to_thread(HookInterceptor.safe_fire, context)
        except Exception:
            log.exception(
                context.event.value,
                extra={
                    "msg": "async_safe_fire_触发失败",
                    "data": {"event": json.dumps(context, default=str, ensure_ascii=False)},
                },
            )
            return None
