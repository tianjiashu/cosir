"""工具展示提示值对象（ToolDisplayHints）。

本模块承载「工具在前端应如何展示」的语义元数据，是 ``ToolDefinition``
契约的一部分，也是不同工具展示差异收敛到后端的唯一事实来源。前端只保留
一个通用渲染引擎，按本对象投影出的字段做渲染，不按工具名写特化分支。

设计边界：
- 零依赖（不 import ``app.*`` 之外的业务模块），避免循环依赖与包初始化污染。
- 渲染逻辑完全由 handler 注入的纯函数承担，不再保留「模板字符串 + 逃生舱函数」
  两条并存路径；统一的渲染函数签名为 ``Callable[[dict], str | None]``，纯函数、
  不抛异常、缺失字段自行降级。
- 通用字段派生（分页行号 / 路径 basename）收进模块级共享纯函数
  ``derive_display_fields``，渲染入口在投影前统一补齐，供各 handler 的渲染函数
  直接消费，避免重复实现。
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class ToolDisplayHints:
    """工具在前端如何展示的契约元数据。

    一个工具「应该强调哪些参数、如何概括为一句话、点击后做什么」都收敛到这里，
    而不是散落到前端各组件。渲染统一由 handler 注入的纯函数完成，不再提供
    模板字符串或逃生舱两种并存路径。

    参数:
        verb: 动作名，如 “读取”、“搜索”，前端作为主标题动词。
        icon: lucide 图标名，如 “eye”、“search”，前端据此渲染图标。
        title_summary: 标题摘要，前端使用。
        content_summary: 内容摘要，前端展开内容，包含成功或者失败。
        expandable: 是否可展开；默认 ``True``，`前端据此隐藏展开箭头。
        expand_layout: 展开态布局；默认 ``"details"``（通用 key=value 兜底）。可选
            ``none`` / ``details`` / ``list`` / ``diff`` / ``write`` / ``terminal``，
            前端仅按该字符串分发布局，不按工具名写特化分支。

    返回:
        无。

    异常:
        无。

    副作用:
        无（frozen dataclass，不可变）。
    """

    verb: str
    icon: str
    title_summary: Callable[[dict[str, Any]], str] | None = None
    result_summary: Callable[[dict[str, Any]], str | None] | None = None
    expandable: bool = True
    expand_layout: str = "details"

    def render_request(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        """把一次工具调用参数投影成前端可读的展示字典。

        渲染入口先经 ``derive_display_fields`` 补齐通用派生字段，再交由
        ``render_summary`` 纯函数投影摘要（缺失或异常时降级为 ``verb + 主参数``）；
        点击动作由 ``click_action`` 模板解析。基础字典后统一追加 ``expandable`` /
        ``expand_layout``。

        参数:
            arguments: 本次工具调用的实际参数字典。

        返回:
            含 ``verb`` / ``icon`` / ``summary`` / ``detail_keys`` / ``click_action``
            / ``expandable`` / ``expand_layout`` 的字典，可直接序列化进事件 payload
            由前端通用渲染。

        异常:
            不向上抛出；``render_summary`` / ``click_action`` 解析失败均安全降级。

        副作用:
            无。
        """
        title_summary = self.title_summary(arguments)
        return {
            "verb": self.verb,
            "icon": self.icon,
            "title_summary": title_summary,
            "expandable": self.expandable,
            "expand_layout": self.expand_layout,
        }



    def render_result_summary(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """把执行后观察的结构化 ``data`` 投影成一行结果摘要。

        参数:
            data: ``ToolObservation.data`` 结构化载荷字典。

        返回:
            结果摘要文本（如 ``已读取 main.py · 120 行``）；未声明
            ``render_result_summary`` 或投影返回空时返回 ``None``
            （前端降级为执行前摘要）。

        异常:
            不向上抛出；投影函数异常一律返回 ``None``。

        副作用:
            无。
        """
        result_summary = self.result_summary(data)
        if result_summary is None:
            return None
        return {
            "result_summary": result_summary,
            "expandable": True,
        }
