"""工具展示静态声明值对象（ToolDisplayHints）。

本模块只承载「工具在客户端长什么样」的**静态声明**，是 ``ToolDefinition`` 契约的
一部分。后端不承载任何渲染逻辑：摘要文本、列表条目、diff 条目等一律由客户端按
本声明与工具结构化数据渲染（规则在 ``apps/shared/ts/toolDisplayRules.ts``）。

设计边界：
- 只有字面量字段，不含 ``Callable``、不含摘要文本、不含条目投影。
- 零依赖（不 import 业务模块），避免循环依赖与包初始化污染。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolDisplayHints:
    """工具在客户端如何展示的静态声明。

    参数:
        verb: 动作名，如 “读取”、“搜索”，客户端作为主标题动词。
        icon: lucide 图标名，如 “eye”、“search”，客户端据此渲染图标。
        expandable: 是否可展开；默认 ``True``，客户端据此隐藏展开箭头。
        expand_layout: 展开态布局；默认 ``"details"``。可选 ``none`` / ``details`` /
            ``list`` / ``diff`` / ``write`` / ``terminal``，客户端仅按该字符串分发
            布局，不按工具名写特化分支。

    返回:
        无。

    异常:
        无。

    副作用:
        无（frozen dataclass，不可变）。
    """

    verb: str
    icon: str
    expandable: bool = True
    expand_layout: str = "details"
