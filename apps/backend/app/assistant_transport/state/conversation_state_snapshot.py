"""Conversation Transport Session snapshot 的中性 JSON 契约（Session 视角）。

校验（:func:`validate_snapshot`）的字段集合取自各 TypedDict 声明，不另写键集合常量：契约改动只需
改声明，校验跟着走。
"""

from collections.abc import Callable, Iterable
from typing import Literal, NotRequired, cast, get_args

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_activity import (
    ConversationStateActivity,
    ConversationStateLaunch,
    ModelActivity,
)
from app.assistant_transport.state.conversation_state_background_task import (
    ConversationBackgroundTask,
    validate_background_tasks,
    validate_background_work,
    validate_foreground_waits,
)
from app.assistant_transport.state.conversation_state_deliverable import (
    validate_deliverables,
    validate_session_deliverables,
)
from app.assistant_transport.state.conversation_state_message import (
    ConversationQueuedMessage,
    ConversationStateMessage,
    ConversationStateOutcome,
    MessageRole,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import (
    ConversationDeliverable,
    ConversationStateAgentMessagePart,
    ConversationStateImagePart,
    ConversationStateMessageSource,
    ConversationStateReport,
    ConversationStateTextPart,
    ConversationStateToolCallPart,
    TextPartStatus,
    ToolCallStatus,
)
from app.assistant_transport.state.conversation_state_terminal_approval import (
    ConversationTerminalApproval,
    validate_terminal_approvals,
)
from app.assistant_transport.state.conversation_state_terminal_session import (
    ConversationTerminalSession,
    validate_terminal_sessions,
)
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
)
from app.config.constant import Constant
from app.core.events.model_activity_event import ModelActivityPhase
from app.core.tool.presentation import ToolPresentation
from app.core.workflow.message_queue.session_message import Delivery
from app.models.run.run_record import RunError
from app.models.run.run_status import (
    TERMINAL_RUN_STATUSES,
    RunStatus,
)
from app.models.run.run_usage import RunUsage
from app.models.terminal_permissions import TerminalPermissionMode

_RUN_STATUSES = frozenset({status.value for status in RunStatus})
_TERMINAL_RUN_STATUS_VALUES = frozenset({status.value for status in TERMINAL_RUN_STATUSES})
_MESSAGE_ROLES = frozenset(get_args(MessageRole))
_DELIVERIES = frozenset(get_args(Delivery))
_MODEL_ACTIVITY_PHASES = frozenset(get_args(ModelActivityPhase))
_TOOL_PRESENTATIONS = frozenset(get_args(ToolPresentation))
_TOOL_STATUSES = frozenset(get_args(ToolCallStatus))
_TEXT_PART_STATUSES = frozenset(get_args(TextPartStatus))

ForkKind = Literal["user_fork", "agent_fork"]
_FORK_KINDS = frozenset(get_args(ForkKind))
_TERMINAL_PERMISSION_MODES = frozenset(get_args(TerminalPermissionMode))


class ConversationStateFork(TypedDict):
    sourceSessionId: int
    sourceTitle: str
    sessionType: ForkKind
    initialLaunch: ConversationStateLaunch


class ConversationStateSnapshot(TypedDict):
    """一个 Session 的完整 Transport state。

    对话界面只看 Session：``title`` 投影自 ``sessions.title``（事实源在 Session 表，snapshot 只是让
    打开中的页面实时收到标题变化），``messages`` 按回合扁平排列（每个回合：开启它的输入 +
    一条回复），``activity`` 是最新 Run 的状态摘要。Run 身份不进 wire，历史回复的结局由消
    息上的 ``outcome`` 承载。命令被拒不进 snapshot，由命令的 HTTP 响应同步返回。
    ``terminal_sessions`` 是本 Session 终端会话的进程内事实（不持久化），投影规则见
    ``ConversationStateService.refresh_terminal_sessions``。
    ``background_tasks`` 的历史来自持久化占位，活状态来自后台结果登记表；
    ``foreground_waits``（可转到后台的前台等待）是进程内事实，
    二者见 ``refresh_background_results``。
    ``deliverables`` 是会话交付清单（侧栏「产出」卡），由工作副本的工具 part 派生，见
    ``project_session_deliverables``。
    ``terminal_permission_mode`` 投影自 ``sessions.terminal_permission_mode``（会话的终端权限，随时可切、
    下一条命令生效），选择器只读它。

    ``messageOffset`` 是 ``messages`` 之前还有多少条消息没带上；
    后端工作副本始终是完整历史（恒为 0），
    只有发给前端的完整快照按窗口裁剪（见 ``snapshot_window``）。增量路径里的消息下标始终是完整
    历史里的位置；位置只在完整快照处才会变（投影只在末尾追加或原位替换消息），前端据此换算。
    """

    fork: NotRequired[ConversationStateFork]
    queued_messages: list[ConversationQueuedMessage]
    title: str
    messages: list[ConversationStateMessage]
    messageOffset: int
    activity: ConversationStateActivity | None
    terminal_sessions: list[ConversationTerminalSession]
    background_tasks: list[ConversationBackgroundTask]
    foreground_waits: list[str]
    terminal_approvals: list[ConversationTerminalApproval]
    deliverables: list[ConversationDeliverable]
    terminal_permission_mode: TerminalPermissionMode


def empty_snapshot(
    title: str = "", terminal_permission_mode: TerminalPermissionMode = "request_approval"
) -> ConversationStateSnapshot:
    """返回新 Session 的空 snapshot 基线；``title`` 投影自 ``sessions.title``，还没起名时为空。"""

    return ConversationStateSnapshot(
        terminal_permission_mode=terminal_permission_mode,
        title=title,
        messages=[],
        messageOffset=0,
        activity=None,
        terminal_sessions=[],
        background_tasks=[],
        foreground_waits=[],
        terminal_approvals=[],
        deliverables=[],
        queued_messages=[],
    )


def validate_snapshot(state: ConversationStateSnapshot) -> None:
    """严格校验 Session snapshot 的结构与数值边界。"""

    snapshot = as_shape(state, ConversationStateSnapshot, "snapshot")
    if "fork" in snapshot:
        _validate_fork(snapshot["fork"])
    require_int(snapshot["messageOffset"], "snapshot messageOffset", minimum=0)
    _validate_title(snapshot["title"])
    seen_message_ids: set[str] = set()
    for queued_message in require_list(snapshot["queued_messages"], "snapshot queued_messages"):
        _validate_queued_message(queued_message, seen_message_ids)
    for message in require_list(snapshot["messages"], "snapshot messages"):
        _validate_message(message, seen_message_ids)
    if snapshot["activity"] is not None:
        _validate_activity(snapshot["activity"])
    validate_terminal_sessions(snapshot["terminal_sessions"])
    validate_background_tasks(snapshot["background_tasks"])
    validate_foreground_waits(snapshot["foreground_waits"])
    validate_terminal_approvals(snapshot["terminal_approvals"])
    validate_session_deliverables(snapshot["deliverables"])
    _validate_terminal_permission_mode(snapshot["terminal_permission_mode"])


def _validate_terminal_permission_mode(value: object) -> None:
    require_one_of(value, _TERMINAL_PERMISSION_MODES, "snapshot terminal permission mode")


def validate_mutated(
    state: ConversationStateSnapshot, mutations: Iterable[ConversationStateMutation]
) -> None:
    """按变更触及的范围校验工作副本：工作副本物化时整份校验过，之后每次变更只需守住它碰过的部分。

    - ``append-text`` 只加长一个已有字符串，不会改变形状，不校验（流式 token 与终端输出的热路径）；
    - 只改某条消息内部（路径深入到消息之下、且不是消息 id）：只校验这条消息；
    - 与消息无关的顶层字段（标题、当前活动、终端会话、看板、前台等待、交付清单）：只校验该字段；
    - 其余（整条消息替换或追加、消息 id、排队消息——它们与消息共用 id 唯一性）：整份校验。

    读取不再校验，所以这里是快照契约在运行期的唯一守卫。

    异常:
        ValueError: 变更后的快照不满足契约。
    """

    message_indexes: set[int] = set()
    fields: set[str] = set()
    for mutation in mutations:
        if mutation.kind == "append-text":
            continue
        path = mutation.path
        if path[0] == "messages" and len(path) >= 3 and path[2] != "id":
            message_indexes.add(int(path[1]))
        elif len(path) >= 1 and path[0] in _INDEPENDENT_FIELD_VALIDATORS:
            fields.add(str(path[0]))
        else:
            validate_snapshot(state)
            return
    for index in message_indexes:
        _validate_message(state["messages"][index], set())
    snapshot = cast(dict[str, object], state)
    for field in fields:
        _INDEPENDENT_FIELD_VALIDATORS[field](snapshot[field])


def _validate_title(value: object) -> None:
    # 允许为空：还没起名的会话没有标题（由标题支线生成），界面统一按「新会话」显示。
    require_str(value, "snapshot title")


def _validate_optional_activity(value: object) -> None:
    if value is not None:
        _validate_activity(value)


def _validate_fork(value: object) -> None:
    fork = as_shape(value, ConversationStateFork, "snapshot fork")
    require_int(fork["sourceSessionId"], "snapshot fork sourceSessionId", minimum=0)
    require_str(fork["sourceTitle"], "snapshot fork sourceTitle")
    require_one_of(fork["sessionType"], _FORK_KINDS, "snapshot fork sessionType")
    _validate_launch(fork["initialLaunch"])


def _validate_activity(value: object) -> None:
    activity = as_shape(value, ConversationStateActivity, "snapshot activity")
    require_one_of(activity["status"], _RUN_STATUSES, "snapshot activity status")
    require_int(activity["turn"], "snapshot activity turn", minimum=1, nullable=True)
    require_bool(activity["resumable"], "snapshot activity resumable")
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
    require_non_empty_str(activity["startedAt"], "snapshot model activity startedAt")
    if run_status != RunStatus.RUNNING.value:
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
    require_str(message["clientMessageId"], "queued message clientMessageId", nullable=True)


def _validate_message(value: object, seen_message_ids: set[str]) -> None:
    message = as_shape(value, ConversationStateMessage, "snapshot message")
    _validate_message_fields(message["id"], message["role"], message["parts"], seen_message_ids)
    require_int(message["turn"], "snapshot message turn", minimum=1)
    require_bool(message["inherited"], "snapshot message inherited")
    if message["outcome"] is not None:
        if message["role"] != "assistant":
            raise ValueError("only assistant messages carry an outcome")
        _validate_outcome(message["outcome"])
    if message["source"] is not None:
        if message["role"] != "user":
            raise ValueError("only user messages carry a source")
        _validate_source(message["source"])
    if message["clientMessageId"] is not None:
        if message["role"] != "user":
            raise ValueError("only user messages carry a clientMessageId")
        require_non_empty_str(message["clientMessageId"], "snapshot message clientMessageId")


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
    require_int(source["sessionId"], "snapshot message source sessionId", minimum=0)
    require_str(source["name"], "snapshot message source name")
    report = source["report"]
    if report is not None:
        report = as_shape(report, ConversationStateReport, "snapshot message source report")
        require_one_of(
            report["status"], frozenset({"completed", "failed"}), "snapshot report status"
        )
        require_bool(report["intermediate"], "snapshot report intermediate")


def _validate_launch(value: object) -> None:
    launch = as_shape(value, ConversationStateLaunch, "snapshot launch")
    require_str(launch["modelConfigId"], "snapshot launch modelConfigId", nullable=True)
    require_str(launch["reasoningEffort"], "snapshot launch reasoningEffort", nullable=True)


def _validate_usage(value: object) -> None:
    usage = as_shape(value, RunUsage, "snapshot usage")
    for field, amount in usage.items():
        require_int(amount, f"snapshot usage {field}", minimum=0, nullable=True)


def _validate_error(value: object) -> None:
    # 错误契约只允许 code + message。
    # 两者都必须是**非空白**字符串：空白文案会让前端渲染出没有内容的错误气泡。
    error = as_shape(value, RunError, "snapshot error")
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
    require_bool(tool_part["awaitingUser"], "snapshot tool awaitingUser")
    # 其余字段都是 NotRequired：只有出现时才校验。
    if "error" in tool_part:
        require_str(tool_part["error"], "snapshot tool error", nullable=True)
    if "args" in tool_part:
        require_mapping(tool_part["args"], "snapshot tool args", nullable=True)
    if "display_data" in tool_part:
        require_mapping(tool_part["display_data"], "snapshot tool display_data", nullable=True)
    if "startedAt" in tool_part and tool_part["startedAt"] is not None:
        require_non_empty_str(tool_part["startedAt"], "snapshot tool startedAt")
    if "terminal_output_seq" in tool_part:
        require_int(
            tool_part["terminal_output_seq"], "snapshot tool terminal_output_seq", minimum=0
        )
    if "background" in tool_part:
        validate_background_work(tool_part["background"])
    if "deliverables" in tool_part:
        validate_deliverables(tool_part["deliverables"])


# 与消息无关、可单独校验的顶层字段（见 :func:`validate_mutated`）。
_INDEPENDENT_FIELD_VALIDATORS: dict[str, Callable[[object], None]] = {
    "title": _validate_title,
    "activity": _validate_optional_activity,
    "terminal_sessions": validate_terminal_sessions,
    "background_tasks": validate_background_tasks,
    "foreground_waits": validate_foreground_waits,
    "terminal_approvals": validate_terminal_approvals,
    "deliverables": validate_session_deliverables,
    "terminal_permission_mode": _validate_terminal_permission_mode,
}
