"""Hook 注册表与执行门面。

单一职责：按 ``event`` 索引已注册的 ``HookBase`` 实例，并在触发时编排执行。
它兼任「索引」与「执行」两个职责——这是经过方案审查确认的收敛设计
（``Hook机制技术方案.md`` §二），拆分阈值见下。

失败安全语义：任何 Hook 抛异常、超时、或返回非法结果，均不阻断主流程，
统一兜底为 ``ALLOW`` 并写 error 日志（含 hook 名与事件，便于定位）。
"""

from __future__ import annotations

from app.config.logging.logger import log
from app.hook.hook_base import HookBase
from app.hook.hook_context import HookContext
from app.hook.hook_event import HookDecision, HookEvent
from app.hook.hook_result import HookResult

# 进程级单例。运行期只读（注册只在启动期单线程播种，见 bootstrap_hooks），
# 因此无需加锁；__init__ 也保持无锁，避免运行期初始化竞态（AGENTS.md 约定）。
_registry: HookRegistry | None = None


class HookRegistry:
    """按事件索引并编排执行 Hook 的注册表。

    职责边界：索引（register / resolve_for / list）+ 执行（fire）。不持有业务状态、
    不依赖 service / tools 执行层、不解析配置（本机制无配置层，决策 D3）。

    拆分阈值（``Hook机制技术方案.md`` §二）：当订阅方出现「执行前需做 A、B、C
    三类横切校验」且逻辑可独立成模块时，把 ``fire`` 中的编排抽成独立的
    ``HookExecutor``；当注册来源从「启动期硬编码」扩展为「多来源动态注册」时，
    把索引抽成独立的 ``HookIndex``。当前订阅方仅 1 个内置 Hook，未达阈值。
    """

    def __init__(self) -> None:
        """构造空注册表。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            初始化 ``_subs``（按事件分组的 Hook 列表）与 ``_seeded`` 标记。
            不加锁——单例在启动期单线程播种，运行期只读（见模块级 ``_registry``）。
        """
        self._subs: dict[HookEvent, list[HookBase]] = {}
        self._seeded = False

    def register(self, hook: HookBase) -> None:
        """注册一个 Hook（按 ``hook.event`` 分组 append，同事件按注册顺序）。

        参数:
            hook: 待注册的 ``HookBase`` 实例。

        返回:
            无。

        异常:
            无（``matcher`` 的正则编译已在 ``HookBase.__init__`` 完成并 fail-fast，
            此处不再重复校验）。

        副作用:
            向 ``self._subs[hook.event]`` 追加该 Hook。运行期调用会扩大订阅列表，
            当前机制约定只在启动期播种，运行期只读（见 ``bootstrap_hooks``）。
        """
        self._subs.setdefault(hook.event, []).append(hook)

    def resolve_for(self, event: HookEvent) -> list[HookBase]:
        """返回某事件下所有已注册的 Hook（未注册事件返回空列表）。

        参数:
            event: 待解析的触发时机。

        返回:
            该事件下按顺序注册的 ``HookBase`` 列表；无订阅时返回空列表（``fire``
            据此零开销直接放行）。

        异常:
            无。

        副作用:
            无。
        """
        return self._subs.get(event, [])

    def list(self) -> list[HookBase]:
        """列举全部已注册 Hook（跨事件、按事件分组顺序合并）。

        参数:
            无。

        返回:
            所有已注册 ``HookBase`` 的扁平列表。

        异常:
            无。

        副作用:
            无。
        """
        result: list[HookBase] = []
        for hooks in self._subs.values():
            result.extend(hooks)
        return result

    def fire(self, context: HookContext) -> HookResult:
        """触发某事件下所有 Hook，按失败安全语义聚合结果。

        编排规则：
        - 取 ``context.event`` 下全部 Hook；
        - 依次 ``matches(context)`` 过滤，未命中跳过；
        - 命中者 ``execute(context)``；任一抛异常 / 超时 → 兜底 ``ALLOW`` 并记 error；
        - 首个 ``DENY`` 立即短路返回（后续 Hook 不再执行）；
        - ``PRE_TOOL_USE`` 下首个 ``modified_arguments`` 生效（覆盖式，后者覆盖前者）；
        - 空订阅列表 → 直接返回 ``ALLOW``（零开销）。

        参数:
            context: 触发上下文（只读，Hook 不得修改）。

        返回:
            聚合后的 ``HookResult``：``decision`` 为首个 DENY 或最终 ALLOW；
            ``modified_arguments`` 为命中 Hook 中最后一个非空的改写值；
            ``additional_context`` 为所有命中 Hook 的非空 ``additional_context`` 拼接
            （换行分隔，首版无消费方，仅作通道占位，见 ``Hook机制技术方案.md`` §6.3）。

        异常:
            无（任何失败均兜底为 ``ALLOW``）。

        副作用:
            可能写 error / warning 日志（Hook 异常或 DENY）；不修改 ``context``。
        """
        hooks = self.resolve_for(context.event)
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


def initialize_hook_registry() -> HookRegistry:
    """播种进程级单例。

    必须在应用启动期（单线程）调用一次：构造空注册表、播种内置 Hook，
    再赋给模块级 ``_registry``。运行期不得再次调用（会重置订阅）。

    参数:
        无。

    返回:
        已播种的 ``HookRegistry`` 单例。

    异常:
        无。

    副作用:
        覆盖模块级 ``_registry``（启动期仅一次）；调用 ``bootstrap_hooks`` 注册
        内置 Hook。日志同时落 app 日志与（若已装备）sqlite 日志库。
    """
    global _registry
    reg = HookRegistry()
    from app.hook.builtins.bootstrap_hooks import bootstrap_hooks

    bootstrap_hooks(reg)
    _registry = reg
    log.info(
        "hook_registry_initialized",
        extra={
            "msg": "Hook 注册表已初始化",
            "data": {"hook_count": len(reg.list())},
        },
    )
    return reg


def get_hook_registry() -> HookRegistry:
    """获取进程级 Hook 注册表单例。

    参数:
        无。

    返回:
        已初始化的 ``HookRegistry`` 单例。

    异常:
        RuntimeError: 在 ``initialize_hook_registry`` 之前被调用（调用顺序错误）。
            采用 fail-fast 语义，对齐 ``Hook机制技术方案.md`` §6.4 与
            ``app.config.configuration.get_agent_registry`` 的一致约定——未初始化
            即调用属装配错误，应尽早暴露而非静默返回空注册表（否则 Hook 静默失效、
            难以排查）。

    副作用:
        无。
    """
    global _registry
    if _registry is None:
        raise RuntimeError(
            "get_hook_registry 在 initialize_hook_registry 之前被调用，" "请检查应用启动顺序"
        )
    return _registry
