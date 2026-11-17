"""工具调用及其生命周期的 UI part 契约。"""

# 不用 ``from __future__ import annotations``：运行期校验（``wire_shape.as_shape``）从 TypedDict 的
# ``__required_keys__`` 读必填字段，字符串化的注解会让 ``NotRequired`` 字段被当成必填。
from typing import Literal, NotRequired

from typing_extensions import TypedDict

from app.core.tool.presentation import ToolPresentation
from app.models.enums.tool_call_status import ToolCallEventStatus
from app.models.file_kind import FileKind
from app.models.message_metadata import BackgroundKind

# 与事件侧共用同一事实来源，避免两处字面量漂移。
ToolCallStatus = ToolCallEventStatus


class ConversationBackgroundWork(TypedDict):
    """后台占位上的工作记录（``BackgroundWork`` 的 wire 形状）。卡片只看它在不在；看板从工作副本里
    的这些记录投影，所以它们随 part 进入快照，看板不另外查库。"""

    kind: BackgroundKind
    label: str
    startedAt: str
    endedAt: str | None


class ConversationDeliverable(TypedDict):
    """一项交付记录的 wire 形状（驼峰）。按 ``type`` 带不同字段：文件有 ``workspacePath``、
    ``absolutePath``、``kind``、``size``、``width``、``height``；目录有前两者；网址有 ``url``。
    ``key`` 是后端算好的判等值（规范路径或规范化网址），前端去重只比较它，不重复实现规范化。"""

    type: Literal["file", "directory", "url"]
    key: str
    caption: str | None
    workspacePath: NotRequired[str | None]
    absolutePath: NotRequired[str]
    kind: NotRequired[FileKind]
    size: NotRequired[int]
    width: NotRequired[int | None]
    height: NotRequired[int | None]
    url: NotRequired[str]


class ConversationStateToolCallPart(TypedDict):
    """工具调用及其生命周期的 UI part。

    描述一次工具调用从待执行到完成/失败/取消的完整生命周期，并携带参数
    与可能的错误信息。status 取值受限为 ToolCallEventStatus（见
    ``app.models.enums.tool_call_status``）的五个枚举。
    """

    type: Literal["tool-call"]
    toolCallId: str
    toolName: str
    presentation: ToolPresentation
    status: ToolCallStatus
    # 「等你操作」：占位行 ``pending`` 且不是后台占位（HITL 等决定、交接等交还）。建 part 时为 ``False``，
    # 由 ``project_tool_result`` 在每次结果投影时显式写，决定到达、重放后清回 ``False``；冷重建再按
    # 行级规则（``awaits_user``：继承副本与撤回移出的调用不能决定）纠正一次。失败与否看 ``status``。
    awaitingUser: bool
    args: NotRequired[dict[str, object]]
    intent: NotRequired[str | None]
    error: NotRequired[str | None]
    # 图标、动词与调用行标题都由前端按 ``intent`` / ``toolName`` / ``args`` 查工具目录表得出，
    # part 不带展示外壳。
    display_data: NotRequired[dict[str, object] | None]
    # 后台占位的工作记录：存在即后台占位，``pending`` 即「后台执行中」；投影自占位行 metadata。
    background: NotRequired[ConversationBackgroundWork]
    # 交付记录（只有 ``present_deliverables``）：投影自结果行 metadata，回合聚合卡与产出清单都读它。
    deliverables: NotRequired[list[ConversationDeliverable]]
    # 进入 ``running`` 的时刻（ISO 8601，取自状态事件），前端据此本地计时运行时长；其余时候为
    # ``None``（建 part、冷重建与离开 running 时）。冷重建没有运行中的调用，所以恒为 ``None``。
    startedAt: NotRequired[str | None]
    # 运行期终端输出的序号，挡住陈旧或重复的输出块。
    terminal_output_seq: NotRequired[int]
