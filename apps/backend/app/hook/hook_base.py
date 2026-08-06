"""Hook 基类（内置与未来 Hook 的共同抽象）。

单一职责：定义「一个 Hook 是什么、如何被触发、如何被匹配」的抽象契约。
它不持有注册表、不负责执行编排（那是 ``HookRegistry`` 的职责），也不依赖任何
业务层。子类只需实现 ``execute`` 并声明 ``event`` / ``matcher`` / ``name``。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from app.hook.hook_context import HookContext
from app.hook.hook_event import HookEvent
from app.hook.hook_result import HookResult


class HookBase(ABC):
    """所有 Hook 的抽象基类。

    子类约定：
    - 在 ``__init__`` 中调用 ``super().__init__(event, matcher)`` 并固化
      ``name`` / ``event`` / ``matcher`` 三个只读属性；
    - 实现 ``execute(context)`` 返回 ``HookResult``。

    ``matcher`` 的适用范围：只匹配 ``tool_name``，因此**只对 ``PRE_TOOL_USE`` /
    ``POST_TOOL_USE`` 有意义**。非工具事件（``USER_PROMPT_SUBMIT`` / ``STOP`` /
    ``SESSION_START`` / ``SESSION_END`` / ``PRE_COMPACT``）的 ``tool_name`` 恒为
    ``None``，此时非空 ``matcher`` 恒不命中，该事件的 Hook 必须留空 ``matcher``。
    此行为是有意设计而非缺陷（``Hook机制技术方案.md`` §6.1）。
    """

    def __init__(self, event: HookEvent, matcher: str | None = None) -> None:
        """初始化基类并预编译 ``matcher`` 正则。

        参数:
            event: 本 Hook 监听的触发时机（必填）。
            matcher: 可选的工具名正则（仅对工具类事件有效）；编译失败立即抛
                ``ValueError``（fail-fast：这是开发者编码错误，须在注册期暴露）。

        返回:
            无。

        异常:
            ValueError: ``matcher`` 无法被 ``re.compile`` 解析时抛出。

        副作用:
            固化 ``_event`` / ``_matcher`` / ``_compiled``（预编译正则）三个只读属性。
        """
        self._event = event
        self._matcher = matcher
        self._compiled = re.compile(matcher) if matcher else None

    @property
    def name(self) -> str:
        """Hook 的稳定标识（子类应覆盖，默认用类名）。

        参数:
            无。

        返回:
            本 Hook 的标识字符串，默认取类名。
        """
        return self.__class__.__name__

    @property
    def event(self) -> HookEvent:
        """本 Hook 监听的触发时机。

        参数:
            无。

        返回:
            绑定的 ``HookEvent`` 枚举值。
        """
        return self._event

    @property
    def matcher(self) -> str | None:
        """工具名匹配正则（仅工具类事件有效，见类 docstring）。

        参数:
            无。

        返回:
            构造时传入的正则字符串，未传为 ``None``。
        """
        return self._matcher

    def matches(self, context: HookContext) -> bool:
        """判断本次触发是否命中本 Hook 的 matcher。

        参数:
            context: 当前触发上下文；仅取 ``context.tool_name`` 做正则匹配。

        返回:
            ``matcher`` 为空 → 恒命中（该事件下所有触发都执行本 Hook）；
            ``matcher`` 非空 → ``re.search(matcher, tool_name or "")`` 命中才返回真。
            非工具事件 ``tool_name`` 为 ``None``，非空 matcher 恒不命中。

        异常:
            无。

        副作用:
            无。
        """
        if self._compiled is None:
            return True
        return self._compiled.search(context.tool_name or "") is not None

    @abstractmethod
    def execute(self, context: HookContext) -> HookResult:
        """执行 Hook 逻辑，返回决策。

        参数:
            context: 触发上下文（只读）。子类只读访问，勿修改。

        返回:
            ``HookResult``：``ALLOW`` 放行、``DENY`` 拒绝（带 ``deny_reason``）、
            或 ``PRE_TOOL_USE`` 下 ``ALLOW`` 并携带 ``modified_arguments`` 改写参数。

        异常:
            子类应自行捕获内部异常并返回 ``HookResult.allow()``（失败安全）；
            基类不强制，但抛出未捕获异常会被 ``HookRegistry.fire`` 兜底为 ALLOW。

        副作用:
            允许写日志（禁止记录 secret / 敏感个人信息）；不得修改 ``context``。
        """
