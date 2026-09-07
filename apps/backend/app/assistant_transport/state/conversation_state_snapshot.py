"""Conversation Transport state 的中性 JSON 契约。"""

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_error import ConversationStateError
from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_run import ConversationStateRun
from app.assistant_transport.state.conversation_state_usage import ConversationStateUsage


class ConversationStateSnapshot(TypedDict):
    """一个 Task 的完整 Transport state。"""

    # 有序消息列表
    messages: list[ConversationStateMessage]
    run: ConversationStateRun
    approvals: dict[str, object]
    context_usage: float
    usage: ConversationStateUsage
    error: ConversationStateError | None


def validate_snapshot(state: ConversationStateSnapshot) -> None:
    """运行时校验固定 snapshot 结构与审批空预留。

    TypedDict 无法在静态层面约束"key 集合恰好为四元组"等运行时事实，故以本函数
    在 load/hydrate/stage 边界统一断言。校验失败抛 ``ValueError``，调用方据此判定
    snapshot 已被破坏或构造非法。

    参数:
        state: 待校验的 snapshot（运行期即为普通 dict）。

    返回:
        无。校验通过即返回；不返回修改后的副本。

    异常:
        ValueError: 当 key 集合、messages/approvals 类型、run 结构、message 角色、
            part 列表或 tool-call 状态不合法，或 approvals 非空（审批能力尚未实现）时抛出。

    副作用:
        无（纯只读断言，不修改 ``state``）。
    """

    expected_keys = {"messages", "run", "approvals", "context_usage", "usage", "error"}
    if not isinstance(state, dict) or set(state) != expected_keys:
        raise ValueError(
            "snapshot must contain messages, run, approvals, context_usage, usage and error"
        )
    if not isinstance(state["messages"], list) or not isinstance(state["approvals"], dict):
        raise ValueError("snapshot messages and approvals must be arrays/object")
    if (
        not isinstance(state["context_usage"], int | float)
        or isinstance(state["context_usage"], bool)
        or state["context_usage"] < 0
    ):
        raise ValueError("snapshot context_usage must be a non-negative number")
    usage = state["usage"]
    expected_usage_keys = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "reasoning_tokens",
    }
    if (
        not isinstance(usage, dict)
        or set(usage) != expected_usage_keys
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in usage.values()
        )
    ):
        raise ValueError("snapshot usage is malformed")
    if state["approvals"]:
        raise ValueError("approval state is reserved and must remain empty")
    if not isinstance(state["run"], dict) or not isinstance(state["run"].get("status"), str):
        raise ValueError("snapshot run is malformed")
    for message in state["messages"]:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            raise ValueError("snapshot contains an invalid message")
        if not isinstance(message.get("parts"), list):
            raise ValueError("snapshot message parts must be an array")
        for part in message["parts"]:
            if isinstance(part, dict) and part.get("type") == "tool-call":
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
                if not isinstance(part.get("presentation", {}), dict):
                    raise ValueError("snapshot tool presentation must be an object")
                if part.get("data") is not None and not isinstance(part.get("data"), dict):
                    raise ValueError("snapshot tool data must be an object or null")


def empty_snapshot() -> ConversationStateSnapshot:
    """返回新 Task 的空 snapshot 基线。

    结构固定为 messages 空、run 未开始（runId 为 None、status 为 idle）、
    approvals 空、error 为 None。供首次创建或读取缺失时作为占位基线，确保
    snapshot 始终满足 ``validate_snapshot`` 的契约。

    参数:
        无。

    返回:
        新构造的空 snapshot（运行期为普通 dict）。

    异常:
        无。

    副作用:
        无（每次调用返回独立新对象，调用方不得共享同一引用）。
    """

    return {
        "messages": [],
        "run": {"runId": None, "status": "idle"},
        "approvals": {},
        "context_usage": 0.0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "reasoning_tokens": 0,
        },
        "error": None,
    }
