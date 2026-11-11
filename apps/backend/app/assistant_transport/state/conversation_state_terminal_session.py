"""Task 终端会话列表的 Transport 契约（snapshot 顶层 ``terminal_sessions`` 的条目）。

事实源是进程内的 ``TerminalSessionService``（``task_runtimes`` 各会话的 ``snapshot()``，由
:func:`to_terminal_session` 映射成本契约的 camelCase 条目）：会话不持久化、不跨后端
重启，所以列表不从 canonical 记录重建，只在 snapshot 物化与会话变化通知时整表读取。终端画面与输入
标记的实时细节走每会话 WebSocket，这里只给工作台标签栏与生命周期所需的摘要。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast, get_args

from typing_extensions import TypedDict

from app.assistant_transport.state.wire_shape import (
    as_shape,
    require_bool,
    require_int,
    require_list,
    require_non_empty_str,
    require_one_of,
    require_str,
)

if TYPE_CHECKING:
    from app.tools.terminal_runtime.session.session_runtime import SessionSnapshot

TerminalSessionStatus = Literal["starting", "running", "exited", "closed", "failed"]
TerminalSessionIntegration = Literal["starting", "ready", "unavailable", "off"]


class ConversationTerminalSession(TypedDict):
    """一个终端会话的摘要；活跃会话在前、按创建时间排序，已结束会话只保留最近若干个。"""

    sessionId: str
    shellKind: str
    status: TerminalSessionStatus
    endReason: str | None
    exitCode: int | None
    integration: TerminalSessionIntegration
    # 集成跟踪到有命令正在执行（started 之后、returned 之前）；无集成时恒为 False，不代表空闲。
    commandRunning: bool
    createdAt: str


_STATUSES = frozenset(get_args(TerminalSessionStatus))
_INTEGRATIONS = frozenset(get_args(TerminalSessionIntegration))


def to_terminal_session(session: SessionSnapshot) -> ConversationTerminalSession:
    """把终端运行时的会话快照映射成 Transport 条目。

    取值词表由 ``validate_terminal_sessions`` 守。
    """

    return ConversationTerminalSession(
        sessionId=session.session_id,
        shellKind=session.shell_kind,
        status=cast(TerminalSessionStatus, session.status),
        endReason=session.end_reason,
        exitCode=session.exit_code,
        integration=cast(TerminalSessionIntegration, session.integration),
        commandRunning=session.command_running,
        createdAt=session.created_at.isoformat(),
    )


def validate_terminal_sessions(value: object) -> None:
    """精确校验 ``terminal_sessions``：字段集合、取值词表与会话身份唯一。

    异常:
        ValueError: 任一条目不满足契约（字段缺失或多余、取值越界、``sessionId`` 重复）。
    """

    seen: set[str] = set()
    for entry in require_list(value, "snapshot terminal_sessions"):
        session = as_shape(entry, ConversationTerminalSession, "snapshot terminal session entry")
        session_id = require_non_empty_str(session["sessionId"], "terminal session sessionId")
        if session_id in seen:
            raise ValueError("snapshot contains duplicate terminal sessionId")
        seen.add(session_id)
        require_str(session["shellKind"], "terminal session shellKind")
        require_str(session["createdAt"], "terminal session createdAt")
        require_one_of(session["status"], _STATUSES, "terminal session status")
        require_one_of(session["integration"], _INTEGRATIONS, "terminal session integration")
        require_bool(session["commandRunning"], "terminal session commandRunning")
        require_str(session["endReason"], "terminal session endReason", nullable=True)
        require_int(session["exitCode"], "terminal session exitCode", nullable=True)
