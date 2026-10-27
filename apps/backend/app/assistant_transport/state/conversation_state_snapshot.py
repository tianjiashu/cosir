"""Conversation Transport Task snapshot 的中性 JSON 契约（Task 视角）。"""

from typing import NotRequired, cast

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_activity import ConversationStateActivity
from app.assistant_transport.state.conversation_state_message import (
    ConversationQueuedMessage,
    ConversationStateMessage,
)
from app.config.constant import Constant
from app.models.enums.conversation_run_status import (
    TERMINAL_RUN_STATUSES,
    ConversationRunStatus,
)

_SNAPSHOT_KEYS = {"messages", "activity"}
_ACTIVITY_KEYS = {
    "runId",
    "status",
    "endReason",
    "error",
    "usage",
    "contextWindowTotal",
    "launch",
}
_MESSAGE_KEYS = {"id", "role", "parts", "runId", "outcome"}
_OUTCOME_KEYS = {"status", "endReason", "error", "usage", "contextWindowTotal"}
_QUEUED_MESSAGE_KEYS = {"id", "role", "parts", "delivery"}
_DELIVERIES = {"immediate", "next_run"}
_RUN_STATUSES = {status.value for status in ConversationRunStatus}
_LAUNCH_KEYS = {"modelConfigId", "reasoningEffort", "banTools"}
_TEXT_PART_KEYS = {"type", "text", "status"}
_IMAGE_PART_KEYS = {"type", "image"}
_FILE_PART_KEYS = {"type", "file", "name", "contentType"}
_TOOL_PART_KEYS = {
    "type",
    "toolCallId",
    "toolName",
    "status",
    "args",
    "error",
    "errorCode",
    "presentation",
    "display_data",
    "team_run_id",
    "isError",
    "approvalRequestId",
    "child_task_id",
    "agent_role",
    "terminal_output_seq",
}
_USAGE_KEYS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "reasoning_tokens",
}


class ConversationStateSnapshot(TypedDict):
    """一个 Task 的完整 Transport state。

    对话界面只看 Task：``messages`` 按 context 序号扁平排列，``activity`` 是最新 Run 的摘要。
    Run 分组不进 wire，历史回复的结局由消息上的 ``outcome`` 承载。命令被拒不进 snapshot，由命令的
    HTTP 响应同步返回。
    """

    queued_messages: NotRequired[list[ConversationQueuedMessage]]
    messages: list[ConversationStateMessage]
    activity: ConversationStateActivity | None


def empty_snapshot() -> ConversationStateSnapshot:
    """返回新 Task 的空 snapshot 基线。"""

    return {"messages": [], "activity": None}


def validate_snapshot(state: ConversationStateSnapshot) -> None:
    """严格校验 Task snapshot 的结构与数值边界。"""

    if not isinstance(state, dict) or set(state) - {"queued_messages"} != _SNAPSHOT_KEYS:
        raise ValueError("snapshot must contain messages and activity")
    if not isinstance(state["messages"], list):
        raise ValueError("snapshot messages must be an array")
    queued = state.get("queued_messages", [])
    if not isinstance(queued, list):
        raise ValueError("queued_messages must be an array")
    seen_ids: set[str] = set()
    for queued_message in queued:
        _validate_message_body(queued_message, seen_ids, _QUEUED_MESSAGE_KEYS)
        if queued_message["delivery"] not in _DELIVERIES:
            raise ValueError("queued message delivery is invalid")
        if queued_message["role"] != "user":
            raise ValueError("queued_messages must contain user messages")
    for message in state["messages"]:
        _validate_message(message, seen_ids)
    if state["activity"] is not None:
        _validate_activity(state["activity"])


def _validate_non_negative_int(value: object, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"snapshot {label} must be a non-negative integer")


def _validate_activity(activity: object) -> None:
    if not isinstance(activity, dict) or set(activity) != _ACTIVITY_KEYS:
        raise ValueError("snapshot activity is invalid")
    _validate_non_negative_int(activity["runId"], "activity runId")
    if activity["status"] not in _RUN_STATUSES:
        raise ValueError("snapshot activity status is invalid")
    if activity["endReason"] is not None and not isinstance(activity["endReason"], str):
        raise ValueError("snapshot activity endReason must be a string or null")
    if activity["contextWindowTotal"] is not None:
        _validate_non_negative_int(activity["contextWindowTotal"], "contextWindowTotal")
    _validate_launch(activity["launch"])
    if activity["usage"] is not None:
        _validate_usage(activity["usage"])
    if activity["error"] is not None:
        _validate_error(activity["error"])


def _validate_outcome(outcome: object) -> None:
    if not isinstance(outcome, dict) or set(outcome) != _OUTCOME_KEYS:
        raise ValueError("snapshot message outcome is invalid")
    if outcome["status"] not in TERMINAL_RUN_STATUSES:
        raise ValueError("snapshot message outcome status must be terminal")
    if outcome["endReason"] is not None and not isinstance(outcome["endReason"], str):
        raise ValueError("snapshot outcome endReason must be a string or null")
    if outcome["usage"] is not None:
        _validate_usage(outcome["usage"])
    if outcome["contextWindowTotal"] is not None:
        _validate_non_negative_int(outcome["contextWindowTotal"], "outcome contextWindowTotal")
    if outcome["error"] is not None:
        _validate_error(outcome["error"])


def _validate_message(message: object, seen_ids: set[str]) -> None:
    _validate_message_body(message, seen_ids, _MESSAGE_KEYS)
    body = cast(dict[str, object], message)
    _validate_non_negative_int(body["runId"], "message runId")
    if body["outcome"] is not None:
        if body["role"] != "assistant":
            raise ValueError("only assistant messages carry an outcome")
        _validate_outcome(body["outcome"])


def _validate_launch(launch: object) -> None:
    if not isinstance(launch, dict) or set(launch) != _LAUNCH_KEYS:
        raise ValueError("snapshot launch is invalid")
    model_config_id = launch["modelConfigId"]
    if model_config_id is not None and (
        not isinstance(model_config_id, int) or isinstance(model_config_id, bool)
    ):
        raise ValueError("snapshot launch modelConfigId must be an integer or null")
    if launch["reasoningEffort"] is not None and not isinstance(launch["reasoningEffort"], str):
        raise ValueError("snapshot launch reasoningEffort must be a string or null")
    if not isinstance(launch["banTools"], list) or not all(
        isinstance(name, str) for name in launch["banTools"]
    ):
        raise ValueError("snapshot launch banTools must be an array of strings")


def _validate_message_body(message: object, seen_message_ids: set[str], keys: set[str]) -> None:
    if not isinstance(message, dict) or set(message) != keys:
        raise ValueError("snapshot contains an invalid message")
    message_id = message["id"]
    if not isinstance(message_id, str) or message_id == "":
        raise ValueError("snapshot message id must be a non-empty string")
    if message_id in seen_message_ids:
        raise ValueError("snapshot contains duplicate message id")
    seen_message_ids.add(message_id)
    if message["role"] not in {"user", "assistant"}:
        raise ValueError("snapshot message role is invalid")
    if not isinstance(message["parts"], list):
        raise ValueError("snapshot message parts must be an array")
    for part in message["parts"]:
        _validate_part(part)


def _validate_usage(usage: object) -> None:
    if not isinstance(usage, dict) or set(usage) != _USAGE_KEYS:
        raise ValueError("snapshot usage is malformed")
    for value in usage.values():
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("snapshot usage is malformed")


def _validate_error(error: object) -> None:
    # 错误契约只允许 code + message。
    # 两者都必须是**非空白**字符串：空白文案会让前端渲染出没有内容的错误气泡。
    if (
        not isinstance(error, dict)
        or set(error) != {"code", "message"}
        or not isinstance(error["code"], str)
        or not error["code"].strip()
        or not isinstance(error["message"], str)
        or not error["message"].strip()
    ):
        raise ValueError("snapshot error is malformed")


def _validate_part(part: object) -> None:
    if not isinstance(part, dict):
        raise ValueError("snapshot message part must be an object")
    part_type = part.get("type")
    if part_type in {"text", "reasoning"}:
        if set(part) - _TEXT_PART_KEYS:
            raise ValueError("snapshot text part contains unknown fields")
        if not isinstance(part.get("text"), str):
            raise ValueError("snapshot text part text must be a string")
        if part.get("status") not in {None, "running", "completed"}:
            raise ValueError("snapshot text part status is invalid")
        return
    if part_type == "image":
        if set(part) != _IMAGE_PART_KEYS:
            raise ValueError("snapshot image part contains unknown fields")
        image = part.get("image")
        if not isinstance(image, str) or not Constant.Transport.IMAGE_LOCATOR.fullmatch(image):
            raise ValueError("snapshot image part locator is invalid")
        return
    if part_type == "file":
        if set(part) != _FILE_PART_KEYS:
            raise ValueError("snapshot file part contains unknown fields")
        if not isinstance(part.get("file"), str) or not Constant.Transport.FILE_LOCATOR.fullmatch(
            part["file"]
        ):
            raise ValueError("snapshot file part locator is invalid")
        if not isinstance(part.get("name"), str) or not part["name"]:
            raise ValueError("snapshot file part name is invalid")
        if not isinstance(part.get("contentType"), str) or not part["contentType"]:
            raise ValueError("snapshot file part contentType is invalid")
        return
    if part_type == "tool-call":
        if set(part) - _TOOL_PART_KEYS:
            raise ValueError("snapshot tool part contains unknown fields")
        if not isinstance(part.get("toolCallId"), str) or not isinstance(part.get("toolName"), str):
            raise ValueError("snapshot tool identity is malformed")
        if part.get("status") not in {
            "pending",
            "running",
            "completed",
            "failed",
            "cancelled",
        }:
            raise ValueError("snapshot contains an invalid tool status")
        if part.get("approvalRequestId") is not None:
            raise ValueError("approval requests are not implemented")
        if part.get("error") is not None and not isinstance(part.get("error"), str):
            raise ValueError("snapshot tool error must be a string or null")
        if part.get("errorCode") is not None and not isinstance(part.get("errorCode"), str):
            raise ValueError("snapshot tool errorCode must be a string or null")
        if part.get("isError") is not None and not isinstance(part.get("isError"), bool):
            raise ValueError("snapshot tool isError must be a boolean")
        if part.get("args") is not None and not isinstance(part.get("args"), dict):
            raise ValueError("snapshot tool args must be an object")
        presentation = part.get("presentation")
        if presentation is not None and not isinstance(presentation, dict):
            raise ValueError("snapshot tool presentation must be an object")
        if part.get("display_data") is not None and not isinstance(part.get("display_data"), dict):
            raise ValueError("snapshot tool display_data must be an object or null")
        if part.get("team_run_id") is not None and (
            not isinstance(part["team_run_id"], int)
            or isinstance(part["team_run_id"], bool)
            or part["team_run_id"] < 1
        ):
            raise ValueError("snapshot team_run_id must be a positive integer")
        if part.get("child_task_id") is not None and (
            not isinstance(part.get("child_task_id"), int)
            or isinstance(part.get("child_task_id"), bool)
            or part["child_task_id"] < 1
        ):
            raise ValueError("snapshot child_task_id must be a positive integer")
        if part.get("agent_role") is not None and (
            not isinstance(part.get("agent_role"), str) or not part["agent_role"].strip()
        ):
            raise ValueError("snapshot agent_role must be a non-empty string")
        if part.get("terminal_output_seq") is not None and (
            not isinstance(part.get("terminal_output_seq"), int)
            or isinstance(part.get("terminal_output_seq"), bool)
            or part["terminal_output_seq"] < 0
        ):
            raise ValueError("snapshot terminal_output_seq must be a non-negative integer")
        return
    raise ValueError("snapshot contains an unknown message part")
