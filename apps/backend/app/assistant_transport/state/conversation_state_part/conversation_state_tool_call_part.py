"""工具调用及其生命周期的 UI part 契约。"""

# 不用 ``from __future__ import annotations``：运行期校验（``wire_shape.as_shape``）从 TypedDict 的
# ``__required_keys__`` 读必填字段，字符串化的注解会让 ``NotRequired`` 字段被当成必填。
from typing import Literal, NotRequired

from typing_extensions import TypedDict

from app.core.tool.presentation import ToolPresentation
from app.models.enums.tool_call_status import ToolCallEventStatus
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
    args: NotRequired[dict[str, object]]
    intent: NotRequired[str | None]
    error: NotRequired[str | None]
    # 图标、动词与调用行标题都由前端按 ``intent`` / ``toolName`` / ``args`` 查工具目录表得出，
    # part 不带展示外壳。
    # 「等你操作」：占位行 ``pending`` 且不是后台占位（HITL 等决定、交接等交还）。只在
    # ``project_tool_result`` 一处计算，每次结果投影都显式写，决定到达、重放后清回 ``False``。
    awaitingUser: NotRequired[bool]
    display_data: NotRequired[dict[str, object] | None]
    # 指向持久化 TeamRun，失败态也能读取节点进度与诊断。
    team_run_id: NotRequired[int]
    # 后台占位的工作记录：存在即后台占位，``pending`` 即「后台执行中」；投影自占位行 metadata。
    background: NotRequired[ConversationBackgroundWork]
    isError: NotRequired[bool]
    # 子 Task 定位：只有委派与分叉把子 Task 提升到 part，
    # 委派行据此读应用级任务状态推送，不在展示数据里镜像子 Run 状态。
    child_task_id: NotRequired[int]
    agent_role: NotRequired[str]
    # 进入 ``running`` 的时刻（ISO 8601，取自状态事件），前端据此本地计时运行时长；其余时候为
    # ``None``（建 part、冷重建与离开 running 时）。冷重建没有运行中的调用，所以恒为 ``None``。
    startedAt: NotRequired[str | None]
    # 运行期终端输出的序号，挡住陈旧或重复的输出块。
    terminal_output_seq: NotRequired[int]
