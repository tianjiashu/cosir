"""Task 终端会话列表的 Transport 契约（snapshot 顶层 ``terminal_sessions`` 的条目）。

事实源是进程内的 ``TerminalSessionService``（``task_runtimes`` 各会话的 ``snapshot()``，由
:func:`to_terminal_session` 映射成本契约的 camelCase 条目）：会话不持久化、不跨后端
重启，所以列表不从 canonical 记录重建，只在 snapshot 物化与会话变化通知时整表读取。终端画面与输入标记
的实时细节走每会话 WebSocket，这里只给工作台标签栏与生命周期所需的摘要。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from typing_extensions import TypedDict

if TYPE_CHECKING:
    from app.tools.terminal_runtime.session_runtime import SessionSnapshot


class ConversationTerminalHandoff(TypedDict):
    """Agent 把终端交给用户输入时的说明；交还（HITL 决定执行）或会话结束后条目上的 ``handoff`` 回到
    ``None``。``toolCallId`` 是那次 ``terminal_handoff`` 调用，前端「交还」即对它作出决定。"""

    reason: str
    toolCallId: str


class ConversationTerminalSession(TypedDict):
    """一个终端会话的摘要；活跃会话在前、按创建时间排序，已结束会话只保留最近若干个。"""

    sessionId: str
    label: str
    shellKind: str
    createdBy: Literal["agent", "user"]
    status: Literal["starting", "running", "exited", "closed", "failed"]
    endReason: str | None
    exitCode: int | None
    integration: Literal["starting", "ready", "unavailable", "off"]
    # 集成跟踪到有命令正在执行（started 之后、returned 之前）；无集成时恒为 False，不代表空闲。
    commandRunning: bool
    handoff: ConversationTerminalHandoff | None
    createdAt: str


def to_terminal_session(session: SessionSnapshot) -> ConversationTerminalSession:
    """把终端运行时的会话快照映射成 Transport 条目；取值枚举由 :func:`validate_terminal_sessions` 守。"""

    return {
        "sessionId": session.session_id,
        "label": session.label or "",
        "shellKind": session.shell_kind,
        "createdBy": session.created_by,
        "status": cast(Literal["starting", "running", "exited", "closed", "failed"], session.status),
        "endReason": session.end_reason,
        "exitCode": session.exit_code,
        "integration": cast(
            Literal["starting", "ready", "unavailable", "off"], session.integration
        ),
        "commandRunning": session.command_running,
        "handoff": (
            {"reason": session.handoff_reason, "toolCallId": session.handoff_call_id}
            if session.handoff_reason is not None and session.handoff_call_id is not None
            else None
        ),
        "createdAt": session.created_at.isoformat(),
    }


TERMINAL_SESSION_KEYS = frozenset(ConversationTerminalSession.__annotations__)
_CREATED_BY = frozenset({"agent", "user"})
_STATUSES = frozenset({"starting", "running", "exited", "closed", "failed"})
_INTEGRATIONS = frozenset({"starting", "ready", "unavailable", "off"})


def validate_terminal_sessions(value: object) -> None:
    """精确校验 ``terminal_sessions``：键集合、取值枚举与会话身份唯一。

    异常:
        ValueError: 任一条目不满足契约（字段缺失或多余、取值越界、``sessionId`` 重复）。
    """

    if not isinstance(value, list):
        raise ValueError("snapshot terminal_sessions must be an array")
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != TERMINAL_SESSION_KEYS:
            raise ValueError("snapshot terminal session entry is invalid")
        session_id = entry["sessionId"]
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("terminal session sessionId must be a non-empty string")
        if session_id in seen:
            raise ValueError("snapshot contains duplicate terminal sessionId")
        seen.add(session_id)
        for key in ("label", "shellKind", "createdAt"):
            if not isinstance(entry[key], str):
                raise ValueError(f"terminal session {key} must be a string")
        if entry["createdBy"] not in _CREATED_BY:
            raise ValueError("terminal session createdBy is invalid")
        if entry["status"] not in _STATUSES:
            raise ValueError("terminal session status is invalid")
        if entry["integration"] not in _INTEGRATIONS:
            raise ValueError("terminal session integration is invalid")
        if not isinstance(entry["commandRunning"], bool):
            raise ValueError("terminal session commandRunning must be a boolean")
        if entry["endReason"] is not None and not isinstance(entry["endReason"], str):
            raise ValueError("terminal session endReason must be a string or null")
        exit_code = entry["exitCode"]
        if exit_code is not None and (
            not isinstance(exit_code, int) or isinstance(exit_code, bool)
        ):
            raise ValueError("terminal session exitCode must be an integer or null")
        handoff = entry["handoff"]
        if handoff is not None and (
            not isinstance(handoff, dict)
            or set(handoff) != {"reason", "toolCallId"}
            or not isinstance(handoff["reason"], str)
            or not isinstance(handoff["toolCallId"], str)
        ):
            raise ValueError("terminal session handoff must be null or {reason, toolCallId}")
