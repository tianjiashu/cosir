"""工具展示静态声明值对象（ToolDisplayHints）。

本模块只承载「工具在客户端长什么样」的**静态声明**，是 ``ToolDefinition`` 契约的
一部分。后端不承载任何渲染逻辑：摘要文本、列表条目、diff 条目等一律由客户端按
本声明与 ``ToolObservation.display_data`` 渲染。

设计边界：
- 只有字面量字段，不含 ``Callable``、不含摘要文本、不含条目投影。
- 零依赖（不 import 业务模块），避免循环依赖与包初始化污染。
"""

from dataclasses import asdict, dataclass
from typing import Literal

ToolDisplaySurface = Literal["trace", "standalone"]
ToolDisplayLayout = Literal["none", "details", "list", "diff", "write", "terminal"]
ToolDisplayVariant = Literal[
    "terminal-session-start",
    "terminal-session-read",
    "terminal-session-write",
    "terminal-session-signal",
    "terminal-session-close",
]


@dataclass(frozen=True)
class ToolDisplayHints:
    """工具在客户端如何展示的静态声明。

    参数:
        verb: 动作名，如 “读取”、“搜索”，客户端作为主标题动词。
        icon: lucide 图标名，如 “eye”、“search”，客户端据此渲染图标。
        variant: 稳定的 renderer 语义变体；用于同一 ``display_data.kind`` 下的
            不同展示形态，不应携带动态结果或用户输入。
        surface: 工具是否属于普通工具轨迹。``trace`` 表示允许客户端将卡片
            折叠进 ``ToolGroup``；``standalone`` 表示卡片应在工具组之外独立展示。
        expandable: 卡片自身是否可展开；默认 ``True``，不决定卡片是否进入
            ``ToolGroup``。
        expand_layout: 卡片自身展开后的布局；客户端仅按该字符串分发布局，不按
            工具名写特化分支。
        default_open: 工具完成后是否默认展开；这是静态偏好，不代表执行状态。
        show_result: 是否把模型可见结果暴露给客户端；关闭时客户端只消费
            ``data``，模型正文仍只进入 RuntimeContext。

    返回:
        无。

    异常:
        无。

    副作用:
        无（frozen dataclass，不可变）。
    """

    verb: str
    icon: str
    variant: ToolDisplayVariant | None = None
    surface: ToolDisplaySurface = "trace"
    expandable: bool = True
    expand_layout: ToolDisplayLayout = "details"
    default_open: bool = False
    show_result: bool = True

    def to_dict(self) -> dict[str, object]:
        """把静态展示声明转换为可跨 Transport 边界传输的普通字典。"""

        return asdict(self)
