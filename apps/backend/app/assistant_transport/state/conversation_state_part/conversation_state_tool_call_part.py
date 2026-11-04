"""工具调用及其生命周期的 UI part 契约。"""

from typing import Literal, NotRequired

from typing_extensions import TypedDict

from app.models.enums.tool_call_status import ToolCallEventStatus

# 与事件侧共用同一事实来源，避免两处字面量漂移。
ToolCallStatus = ToolCallEventStatus


class ConversationBackgroundWork(TypedDict):
    """后台占位上的工作记录（``BackgroundWork`` 的 wire 形状）。卡片只看它在不在；看板从工作副本里
    的这些记录投影，所以它们随 part 进入快照，看板不另外查库。"""

    kind: Literal["terminal_command", "child_agent"]
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
    status: ToolCallStatus
    args: NotRequired[dict[str, object]]
    error: NotRequired[str | None]
    # 图标、动词与调用行标题都由前端按 ``toolName`` + ``args`` 查工具目录表得出，part 不带展示外壳。
    # 「等你操作」：占位行 ``pending`` 且不是后台占位（HITL 等决定、交接等交还）。只在
    # ``project_tool_result`` 一处计算，每次结果投影都显式写，决定到达、重放后清回 ``False``。
    awaitingUser: NotRequired[bool]
    display_data: NotRequired[dict[str, object] | None]
    # 指向持久化 TeamRun，失败态也能读取节点进度与诊断。
    team_run_id: NotRequired[int]
    # 后台占位的工作记录：存在即后台占位，``pending`` 即「后台执行中」；投影自占位行 metadata。
    background: NotRequired[ConversationBackgroundWork]
    isError: NotRequired[bool]
    approvalRequestId: NotRequired[None]
    # 子 Task 定位：只有委派与分叉把子 Task 提升到 part，
    # 委派行据此读应用级任务状态推送，不在展示数据里镜像子 Run 状态。
    child_task_id: NotRequired[int]
    agent_role: NotRequired[str]
    # Runtime terminal output sequence; it guards against stale delta events.
    terminal_output_seq: NotRequired[int]
