"""Hook 注册表（纯索引，执行编排已迁移至 HookInterceptor）。

单一职责：按 ``event`` 索引已注册的 ``HookBase`` 实例，只负责「索引」一维
（``register`` / ``resolve_for`` / ``list``）。「执行编排」已迁至
``app.hook.hook_interceptor.HookInterceptor.fire``（即 Hook 机制的对外拦截收口点），
本模块不再承担触发时的编排职责（拆分见 ``Hook机制技术方案.md`` §二）。

失败安全语义：任何 Hook 抛异常、超时、或返回非法结果，均不阻断主流程，
统一由 ``HookInterceptor.fire`` 兜底为 ``ALLOW`` 并写 error 日志。
"""

from __future__ import annotations

from app.config.logging.logger import log
from app.hook.hook_base import HookBase
from app.hook.hook_event import HookEvent

# 进程级单例。运行期只读（注册只在启动期单线程播种，见 bootstrap_hooks），
# 因此无需加锁；__init__ 也保持无锁，避免运行期初始化竞态（AGENTS.md 约定）。
_registry: HookRegistry | None = None


class HookRegistry:
    """按事件索引 Hook 的注册表（纯索引，不负责执行编排）。

    职责边界：索引（register / resolve_for / list）。执行编排（``fire`` 的失败安全
    聚合：matches 过滤、DENY 短路、modified_arguments 合并、additional_context 拼接）
    已迁至 ``HookInterceptor.fire``，由其对注册表执行 ``resolve_for`` 取 Hook 后编排。
    本类不持有业务状态、不依赖 service / tools 执行层、不解析配置（本机制无配置层，
    决策 D3）。

    拆分依据（``Hook机制技术方案.md`` §二）：当执行编排需「执行前做 A、B、C 三类
    横切校验」且逻辑可独立成模块时，把编排从索引抽离成独立的执行器——即
    ``HookInterceptor``（所有拦截点的统一收口），索引只保留注册与查询。
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
