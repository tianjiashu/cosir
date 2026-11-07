"""Conversation Transport Task snapshot 的中性 JSON 契约（Task 视角）。

校验（:func:`validate_snapshot`）的字段集合取自各 TypedDict 声明，不另写键集合常量：契约改动只需
改声明，校验跟着走。
"""

from typing import Literal, NotRequired, get_args

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_activity import (
    ConversationRunLaunch,
    ConversationStateActivity,
    ModelActivity,
)
from app.assistant_transport.state.conversation_state_background_task import (
    ConversationBackgroundTask,
    validate_background_tasks,
    validate_background_work,
    validate_foreground_waits,
)
from app.assistant_transport.state.conversation_state_error import ConversationStateError
from app.assistant_transport.state.conversation_state_handoff import (
    ConversationHandoff,
    validate_handoffs,
)
from app.assistant_transport.state.conversation_state_message import (
    ConversationQueuedMessage,
    ConversationStateMessage,
    ConversationStateOutcome,
    MessageRole,
)
from app.assistant_transport.state.conversation_state_part import (
    ConversationStateAgentMessagePart,
    ConversationStateFilePart,
    ConversationStateImagePart,
    ConversationStateMessageSource,
    ConversationStateTextPart,
    ConversationStateToolCallPart,
    TextPartStatus,
    ToolCallStatus,
)
from app.assistant_transport.state.conversation_state_terminal_session import (
    ConversationTerminalSession,
    validate_terminal_sessions,
)
from app.assistant_transport.state.conversation_state_usage import ConversationStateUsage
from app.assistant_transport.state.wire_shape import (
    as_shape,
    require_bool,
    require_int,
    require_list,
    require_locator,
    require_mapping,
    require_non_empty_str,
    require_one_of,
    require_str,
    require_str_list,
)
from app.config.constant import Constant
from app.core.events.model_activity_event import ModelActivityPhase
from app.core.tool.presentation import ToolPresentation
from app.core.workflow.message_queue.task_message import Delivery
from app.models.enums.conversation_run_status import (
    TERMINAL_RUN_STATUSES,
    ConversationRunStatus,
)

_RUN_STATUSES = frozenset({status.value for status in ConversationRunStatus})
_TERMINAL_RUN_STATUS_VALUES = frozenset({status.value for status in TERMINAL_RUN_STATUSES})
_MESSAGE_ROLES = frozenset(get_args(MessageRole))
_DELIVERIES = frozenset(get_args(Delivery))
_MODEL_ACTIVITY_PHASES = frozenset(get_args(ModelActivityPhase))
_TOOL_PRESENTATIONS = frozenset(get_args(ToolPresentation))
_TOOL_STATUSES = frozenset(get_args(ToolCallStatus))
_TEXT_PART_STATUSES = frozenset(get_args(TextPartStatus))

ForkKind = Literal["user_fork", "agent_fork"]
_FORK_KINDS = frozenset(get_args(ForkKind))


class ConversationStateFork(TypedDict):
    sourceTaskId: int
    sourceTitle: str
    taskType: ForkKind
    initialLaunch: ConversationRunLaunch


class ConversationStateSnapshot(TypedDict):
    """一个 Task 的完整 Transport state。

    对话界面只看 Task：``title`` 投影自 ``tasks.title``（事实源在 Task 表，snapshot 只是让打开中的
    页面实时收到标题变化），``messages`` 按 context 序号扁平排列，``activity`` 是最新 Run 的摘要。
    Run 分组不进 wire，历史回复的结局由消息上的 ``outcome`` 承载。命令被拒不进 snapshot，由命令的
    HTTP 响应同步返回。``terminal_sessions`` 是本 Task 终端会话的进程内事实（不持久化），
    投影规则见 ``ConversationTaskStateService.refresh_terminal_sessions``。
    ``background_tasks`` 的历史来自持久化占位，活状态来自后台结果登记表；
    ``foreground_waits``（可转到后台的前台等待）是进程内事实，
    二者见 ``refresh_background_results``。
    ``handoffs``（进行中的交接）也是进程内事实，见 ``refresh_handoffs``。

    ``messageOffset`` 是 ``messages`` 之前还有多少条消息没带上；
    后端工作副本始终是完整历史（恒为 0），
    只有发给前端的完整快照按窗口裁剪（见 ``snapshot_window``）。增量路径里的消息下标始终是完整历史
    里的位置；位置只在完整快照处才会变（投影只在末尾追加或原位替换消息），前端据此换算。
    """

    fork: NotRequired[ConversationStateFork]
    queued_messages: NotRequired[list[ConversationQueuedMessage]]
    title: str
    messages: list[ConversationStateMessage]
    messageOffset: int
    activity: ConversationStateActivity | None
    terminal_sessions: list[ConversationTerminalSession]
    background_tasks: list[ConversationBackgroundTask]
    foreground_waits: list[str]
    handoffs: list[ConversationHandoff]


def empty_snapshot(title: str = "新对话") -> ConversationStateSnapshot:
    """返回新 Task 的空 snapshot 基线。"""

    return ConversationStateSnapshot(
        title=title,
        messages=[],
        messageOffset=0,
        activity=None,
        terminal_sessions=[],
        background_tasks=[],
        foreground_waits=[],
        handoffs=[],
    )


def validate_snapshot(state: ConversationStateSnapshot) -> None:
    """严格校验 Task snapshot 的结构与数值边界。"""

    snapshot = as_shape(state, ConversationStateSnapshot, "snapshot")
    if "fork" in snapshot:
        _validate_fork(snapshot["fork"])
    require_int(snapshot["messageOffset"], "snapshot messageOffset", minimum=0)
    require_non_empty_str(snapshot["title"], "snapshot title")
    seen_message_ids: set[str] = set()
    for queued_message in snapshot.get("queued_messages", []):
        _validate_queued_message(queued_message, seen_message_ids)
    for message in require_list(snapshot["messages"], "snapshot messages"):
        _validate_message(message, seen_message_ids)
    if snapshot["activity"] is not None:
        _validate_activity(snapshot["activity"])
    validate_terminal_sessions(snapshot["terminal_sessions"])
    validate_background_tasks(snapshot["background_tasks"])
    validate_foreground_waits(snapshot["foreground_waits"])
    validate_handoffs(snapshot["handoffs"])


def _validate_fork(value: object) -> None:
    fork = as_shape(value, ConversationStateFork, "snapshot fork")
    require_int(fork["sourceTaskId"], "snapshot fork sourceTaskId", minimum=0)
    require_str(fork["sourceTitle"], "snapshot fork sourceTitle")
    require_one_of(fork["taskType"], _FORK_KINDS, "snapshot fork taskType")
    _validate_launch(fork["initialLaunch"])


def _validate_activity(value: object) -> None:
    activity = as_shape(value, ConversationStateActivity, "snapshot activity")
    require_int(activity["runId"], "snapshot activity runId", minimum=0)
    require_one_of(activity["status"], _RUN_STATUSES, "snapshot activity status")
    require_non_empty_str(activity["startedAt"], "snapshot activity startedAt")
    require_str(activity["endReason"], "snapshot activity endReason", nullable=True)
    require_int(
        activity["contextWindowTotal"],
        "snapshot activity contextWindowTotal",
        minimum=0,
        nullable=True,
    )
    if activity["modelActivity"] is not None:
        _validate_model_activity(activity["modelActivity"], activity["status"])
    _validate_launch(activity["launch"])
    if activity["usage"] is not None:
        _validate_usage(activity["usage"])
    if activity["error"] is not None:
        _validate_error(activity["error"])


def _validate_model_activity(value: object, run_status: str) -> None:
    """模型步的瞬时阶段只在 Run 仍在运行时才有意义。"""

    activity = as_shape(value, ModelActivity, "snapshot model activity")
    require_non_empty_str(activity["stepId"], "snapshot model activity stepId")
    require_one_of(activity["phase"], _MODEL_ACTIVITY_PHASES, "snapshot model activity phase")
    if run_status != ConversationRunStatus.RUNNING.value:
        raise ValueError("snapshot model activity requires a running run")


def _validate_outcome(value: object) -> None:
    outcome = as_shape(value, ConversationStateOutcome, "snapshot message outcome")
    if outcome["status"] not in _TERMINAL_RUN_STATUS_VALUES:
        raise ValueError("snapshot message outcome status must be terminal")
    require_str(outcome["endReason"], "snapshot outcome endReason", nullable=True)
    if outcome["usage"] is not None:
        _validate_usage(outcome["usage"])
    require_int(
        outcome["contextWindowTotal"],
        "snapshot outcome contextWindowTotal",
        minimum=0,
        nullable=True,
    )
    require_int(outcome["durationMs"], "snapshot outcome durationMs", minimum=0)
    if outcome["error"] is not None:
        _validate_error(outcome["error"])


def _validate_queued_message(value: object, seen_message_ids: set[str]) -> None:
    message = as_shape(value, ConversationQueuedMessage, "snapshot queued message")
    _validate_message_fields(message["id"], message["role"], message["parts"], seen_message_ids)
    if message["role"] != "user":
        raise ValueError("queued_messages must contain user messages")
    require_one_of(message["delivery"], _DELIVERIES, "queued message delivery")


def _validate_message(value: object, seen_message_ids: set[str]) -> None:
    message = as_shape(value, ConversationStateMessage, "snapshot message")
    _validate_message_fields(message["id"], message["role"], message["parts"], seen_message_ids)
    require_int(message["runId"], "snapshot message runId", minimum=0, nullable=True)
    if message["outcome"] is not None:
        if message["role"] != "assistant":
            raise ValueError("only assistant messages carry an outcome")
        _validate_outcome(message["outcome"])
    if message["source"] is not None:
        if message["role"] != "user":
            raise ValueError("only user messages carry a source")
        _validate_source(message["source"])


def _validate_message_fields(
    message_id: object, role: object, parts: object, seen_message_ids: set[str]
) -> None:
    """消息与排队消息共用的身份、角色与 parts 校验。"""

    checked_id = require_non_empty_str(message_id, "snapshot message id")
    if checked_id in seen_message_ids:
        raise ValueError("snapshot contains duplicate message id")
    seen_message_ids.add(checked_id)
    require_one_of(role, _MESSAGE_ROLES, "snapshot message role")
    for part in require_list(parts, "snapshot message parts"):
        _validate_part(part)


def _validate_source(value: object) -> None:
    source = as_shape(value, ConversationStateMessageSource, "snapshot message source")
    require_int(source["taskId"], "snapshot message source taskId", minimum=0)
    require_str(source["name"], "snapshot message source name")


def _validate_launch(value: object) -> None:
    launch = as_shape(value, ConversationRunLaunch, "snapshot launch")
    require_int(launch["modelConfigId"], "snapshot launch modelConfigId", nullable=True)
    require_str(launch["reasoningEffort"], "snapshot launch reasoningEffort", nullable=True)
    require_str_list(launch["disabledToolGroups"], "snapshot launch disabledToolGroups")


def _validate_usage(value: object) -> None:
    usage = as_shape(value, ConversationStateUsage, "snapshot usage")
    for field, amount in usage.items():
        require_int(amount, f"snapshot usage {field}", minimum=0, nullable=True)


def _validate_error(value: object) -> None:
    # 错误契约只允许 code + message。
    # 两者都必须是**非空白**字符串：空白文案会让前端渲染出没有内容的错误气泡。
    error = as_shape(value, ConversationStateError, "snapshot error")
    require_non_empty_str(error["code"], "snapshot error code")
    require_non_empty_str(error["message"], "snapshot error message")


def _validate_part(part: object) -> None:
    if not isinstance(part, dict):
        raise ValueError("snapshot message part must be an object")
    part_type = part.get("type")
    if part_type == "text":
        _validate_text_part(part)
    elif part_type == "image":
        _validate_image_part(part)
    elif part_type == "file":
        _validate_file_part(part)
    elif part_type == "agent-message":
        _validate_agent_message_part(part)
    elif part_type == "tool-call":
        _validate_tool_call_part(part)
    else:
        raise ValueError("snapshot contains an unknown message part")


def _validate_text_part(part: object) -> None:
    text_part = as_shape(part, ConversationStateTextPart, "snapshot text part")
    require_str(text_part["text"], "snapshot text part text")
    if "status" in text_part:
        require_one_of(text_part["status"], _TEXT_PART_STATUSES, "snapshot text part status")
    if "finalReply" in text_part:
        require_bool(text_part["finalReply"], "snapshot text part finalReply")
    if "sourceSequence" in text_part:
        require_int(text_part["sourceSequence"], "snapshot text part sourceSequence", minimum=0)


def _validate_image_part(part: object) -> None:
    image_part = as_shape(part, ConversationStateImagePart, "snapshot image part")
    require_locator(
        image_part["image"], Constant.Transport.IMAGE_LOCATOR, "snapshot image part locator"
    )


def _validate_file_part(part: object) -> None:
    file_part = as_shape(part, ConversationStateFilePart, "snapshot file part")
    require_locator(
        file_part["file"], Constant.Transport.FILE_LOCATOR, "snapshot file part locator"
    )
    require_non_empty_str(file_part["name"], "snapshot file part name")
    require_non_empty_str(file_part["contentType"], "snapshot file part contentType")


def _validate_agent_message_part(part: object) -> None:
    agent_part = as_shape(part, ConversationStateAgentMessagePart, "snapshot agent message part")
    require_non_empty_str(agent_part["id"], "snapshot agent message part id")
    require_str(agent_part["text"], "snapshot agent message part text")
    _validate_source(agent_part["source"])


def _validate_tool_call_part(part: object) -> None:
    tool_part = as_shape(part, ConversationStateToolCallPart, "snapshot tool part")
    require_one_of(tool_part["presentation"], _TOOL_PRESENTATIONS, "snapshot tool presentation")
    require_str(tool_part["toolCallId"], "snapshot tool toolCallId")
    require_str(tool_part["toolName"], "snapshot tool toolName")
    require_one_of(tool_part["status"], _TOOL_STATUSES, "snapshot tool status")
    # 其余字段都是 NotRequired：只有出现时才校验。
    if "error" in tool_part:
        require_str(tool_part["error"], "snapshot tool error", nullable=True)
    if "intent" in tool_part:
        require_str(tool_part["intent"], "snapshot tool intent", nullable=True)
    if "args" in tool_part:
        require_mapping(tool_part["args"], "snapshot tool args", nullable=True)
    if "display_data" in tool_part:
        require_mapping(tool_part["display_data"], "snapshot tool display_data", nullable=True)
    if "isError" in tool_part:
        require_bool(tool_part["isError"], "snapshot tool isError")
    if "awaitingUser" in tool_part:
        require_bool(tool_part["awaitingUser"], "snapshot tool awaitingUser")
    if "team_run_id" in tool_part:
        require_int(tool_part["team_run_id"], "snapshot tool team_run_id", minimum=1)
    if "child_task_id" in tool_part:
        require_int(tool_part["child_task_id"], "snapshot tool child_task_id", minimum=1)
    if "agent_role" in tool_part:
        require_non_empty_str(tool_part["agent_role"], "snapshot tool agent_role")
    if "terminal_output_seq" in tool_part:
        require_int(
            tool_part["terminal_output_seq"], "snapshot tool terminal_output_seq", minimum=0
        )
    if "background" in tool_part:
        validate_background_work(tool_part["background"])
