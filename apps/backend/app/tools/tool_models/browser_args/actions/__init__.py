"""browser_act 的动作变体：按 ``type`` 区分的联合。

元素用快照里的 ref 指定；任何一次快照里的 ref 在元素还在页面上时都可用，导航后全部失效。
鼠标动作（click / hover / drag 的两端）也可以给视口坐标，覆盖快照里没有的东西（画布、地图、
滑块上的某一点）。宿主按导出的 schema 校验同一份结构，跨字段规则（ref 与坐标二选一等）只在这里。

按输入方式分模块：指针在 :mod:`.pointer`，键盘在 :mod:`.keyboard`，页面状态（选择、滚动、等待、
弹窗、上传）在 :mod:`.page`。``BrowserAction`` 的成员顺序就是 schema 里 ``anyOf`` 的顺序。
"""

from app.tools.tool_models.browser_args.actions.keyboard import PressAction, TypeAction
from app.tools.tool_models.browser_args.actions.page import (
    DialogAction,
    ScrollAction,
    SelectAction,
    UploadAction,
    WaitForAction,
)
from app.tools.tool_models.browser_args.actions.pointer import (
    BrowserPoint,
    ClickAction,
    DragAction,
    HoverAction,
)

BrowserAction = (
    ClickAction
    | HoverAction
    | DragAction
    | TypeAction
    | PressAction
    | SelectAction
    | ScrollAction
    | WaitForAction
    | DialogAction
    | UploadAction
)

__all__ = [
    "BrowserAction",
    "BrowserPoint",
    "ClickAction",
    "DialogAction",
    "DragAction",
    "HoverAction",
    "PressAction",
    "ScrollAction",
    "SelectAction",
    "TypeAction",
    "UploadAction",
    "WaitForAction",
]
