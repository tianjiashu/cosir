"""Conversation Transport Task snapshot 的中性 JSON 契约。"""
import math

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_run_snapshot import ConversationRunSnapshot
from app.assistant_transport.state.conversation_state_error import ConversationStateError
from app.config.constant import Constant

_SNAPSHOT_KEYS = {
    "runs",
    "current_run_id",
    "approvals",
    "context_usage_ratio",
    "context_usage_used",
    "context_window_total",
    "error",
}
_RUN_KEYS = {"runId", "status", "endReason", "messages", "usage", "error"}
_MESSAGE_KEYS = {"id", "role", "parts"}
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
    "isError",
    "approvalRequestId",
    "child_task_id",
    "child_run_id",
    "agent_role",
    "delegation_ref_seq",
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

    Task 只持有当前 context window 测量结果；每个 Run 的消息、生命周期和模型 token
    usage 均位于 ``runs`` 中。该结构是 0-1 契约，不提供旧平铺 snapshot 兼容迁移。
    """

    runs: list[ConversationRunSnapshot]
    current_run_id: int | None
    approvals: dict[str, object]
    context_usage_ratio: float | None
    context_usage_used: int | None
    context_window_total: int | None
    error: ConversationStateError | None


def empty_snapshot() -> ConversationStateSnapshot:
    """返回新 Task 的空 snapshot 基线。"""

    return {
        "runs": [],
        "current_run_id": None,
        "approvals": {},
        "context_usage_ratio": None,
        "context_usage_used": None,
        "context_window_total": None,
        "error": None,
    }


def find_run(state: ConversationStateSnapshot, run_id: int) -> tuple[int, ConversationRunSnapshot]:
    """返回指定 Run 的 ``(index, snapshot)``，不存在时抛出 ``KeyError``。"""

    for index, run in enumerate(state["runs"]):
        if run["runId"] == run_id:
            return index, run
    raise KeyError(f"run {run_id} not found")


def current_run(state: ConversationStateSnapshot) -> ConversationRunSnapshot | None:
    """返回当前 Run snapshot；空 Task 时返回 ``None``。"""

    run_id = state["current_run_id"]
    if run_id is None:
        return None
    _, run = find_run(state, run_id)
    return run


def validate_snapshot(state: ConversationStateSnapshot) -> None:
    """严格校验新 Task snapshot 的结构、归属和数值边界。"""

    if not isinstance(state, dict) or set(state) != _SNAPSHOT_KEYS:
        raise ValueError(
            "snapshot must contain runs, current_run_id, approvals, context_usage_ratio, "
            "context_usage_used, context_window_total and error"
        )
    if not isinstance(state["runs"], list) or not isinstance(state["approvals"], dict):
        raise ValueError("snapshot runs and approvals must be arrays/object")
    current_run_id = state["current_run_id"]
    if current_run_id is not None and (
        not isinstance(current_run_id, int)
        or isinstance(current_run_id, bool)
        or current_run_id < 0
    ):
        raise ValueError("snapshot current_run_id must be a non-negative integer or null")

    ratio = state["context_usage_ratio"]
    if ratio is not None and (
        not isinstance(ratio, int | float)
        or isinstance(ratio, bool)
        or ratio < 0
        or not math.isfinite(ratio)
    ):
        raise ValueError(
            "snapshot context_usage_ratio must be a finite non-negative number or null"
        )
    for field in ("context_usage_used", "context_window_total"):
        value = state[field]
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ValueError(f"snapshot {field} must be a non-negative integer or null")

    seen_run_ids: set[int] = set()
    for run in state["runs"]:
        _validate_run(run, seen_run_ids)
    if current_run_id is not None and current_run_id not in seen_run_ids:
        raise ValueError("snapshot current_run_id must refer to a run")
    if state["approvals"]:
        raise ValueError("approval state is reserved and must remain empty")
    if state["error"] is not None:
        _validate_error(state["error"])


def _validate_run(run: object, seen_run_ids: set[int]) -> None:
    if not isinstance(run, dict) or set(run) != _RUN_KEYS:
        raise ValueError("snapshot contains an invalid run")
    run_id = run["runId"]
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 0:
        raise ValueError("snapshot runId must be a non-negative integer")
    if run_id in seen_run_ids:
        raise ValueError("snapshot contains duplicate runId")
    seen_run_ids.add(run_id)
    if not isinstance(run["status"], str) or run["status"] == "":
        raise ValueError("snapshot run status must be a non-empty string")
    if run["endReason"] is not None and not isinstance(run["endReason"], str):
        raise ValueError("snapshot run endReason must be a string or null")
    if not isinstance(run["messages"], list):
        raise ValueError("snapshot run messages must be an array")
    if run["usage"] is not None:
        _validate_usage(run["usage"])
    if run["error"] is not None:
        _validate_error(run["error"])
    seen_message_ids: set[str] = set()
    for message in run["messages"]:
        _validate_message(message, seen_message_ids)


def _validate_message(message: object, seen_message_ids: set[str]) -> None:
    if not isinstance(message, dict) or set(message) != _MESSAGE_KEYS:
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
        raise ValueError("snapshot run usage is malformed")
    for key, value in usage.items():
        if key == "cache_miss_tokens" and value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("snapshot run usage is malformed")


def _validate_error(error: object) -> None:
    # 错误契约只允许 code + message；``retryable`` 属于工具观察，不进 Transport。
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
        if (
            not isinstance(part.get("file"), str)
            or not Constant.Transport.FILE_LOCATOR.fullmatch(part["file"])
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
        if part.get("child_task_id") is not None and (
            not isinstance(part.get("child_task_id"), int)
            or isinstance(part.get("child_task_id"), bool)
            or part["child_task_id"] < 1
        ):
            raise ValueError("snapshot child_task_id must be a positive integer")
        if part.get("child_run_id") is not None and (
            not isinstance(part.get("child_run_id"), int)
            or isinstance(part.get("child_run_id"), bool)
            or part["child_run_id"] < 1
        ):
            raise ValueError("snapshot child_run_id must be a positive integer")
        if part.get("agent_role") is not None and (
            not isinstance(part.get("agent_role"), str) or not part["agent_role"].strip()
        ):
            raise ValueError("snapshot agent_role must be a non-empty string")
        if part.get("delegation_ref_seq") is not None and (
            not isinstance(part.get("delegation_ref_seq"), int) or part["delegation_ref_seq"] < 0
        ):
            raise ValueError("snapshot delegation_ref_seq must be a non-negative integer")
        if part.get("terminal_output_seq") is not None and (
            not isinstance(part.get("terminal_output_seq"), int)
            or isinstance(part.get("terminal_output_seq"), bool)
            or part["terminal_output_seq"] < 0
        ):
            raise ValueError("snapshot terminal_output_seq must be a non-negative integer")
        return
    raise ValueError("snapshot contains an unknown message part")
