"""browser_* 参数模型共用的字段定义。

同一约束只在源码里写一次：这些描述会进入模型可见 schema，各工具各写一遍必然漂移。
字段对象按需返回新实例（``tab_field``），避免多个模型共享同一个 ``FieldInfo``。
"""

from typing import Any, Literal

from pydantic import Field

TAB_DESCRIPTION = (
    "Tab id such as t1, from an earlier browser result. Pass null to use this session's current tab."
)

# ref 按节点稳定（宿主 ``ref-registry.ts``），任何一次快照里的 ref 在元素还在页面上时都可用，
# 导航后全部失效。
REF_PATTERN = r"^e\d+$"
REF_FIELD = Field(
    pattern=REF_PATTERN,
    description="Element ref such as e12 from a snapshot of the current page.",
)
POINT_REF_DESCRIPTION = (
    "Element ref such as e12 from a snapshot of the current page; its center is used. Leave "
    "null when using x and y."
)

# 鼠标动作也可以给视口坐标，覆盖快照里没有的东西（画布、地图、滑块上的某一点）；
# 坐标与视口截图的像素一致（截图按 CSS 像素出图）。
X_FIELD = Field(
    default=None,
    ge=0,
    description=(
        "Viewport x in CSS pixels, the same as the pixels of a viewport screenshot. Use x and y "
        "instead of ref for spots without a ref, e.g. on a canvas or map."
    ),
)
Y_FIELD = Field(default=None, ge=0, description="Viewport y in CSS pixels; see x.")

MODIFIERS = list[Literal["Alt", "Control", "Meta", "Shift", "ControlOrMeta"]]


def tab_field() -> Any:
    """本 Session 的标签；模型不能指定别的 Session。每次返回新的字段定义，不在模型之间共享实例。"""

    return Field(default=None, pattern=r"^t\d+$", description=TAB_DESCRIPTION)


def one_point(ref: str | None, x: float | None, y: float | None) -> None:
    """鼠标落点：ref 或一对坐标，恰好一种。"""

    if (ref is None) == (x is None and y is None) or (x is None) != (y is None):
        raise ValueError("give either ref, or both x and y")
