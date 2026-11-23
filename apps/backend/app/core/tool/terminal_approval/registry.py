"""待批准请求这份事实：登记、决定、按原因取消，以及给 snapshot 与状态推送的只读视图。

不解析会话树、不停驱动：那是让位规则（``yielding``）的事，这里只管请求本身。
"""

from __future__ import annotations

import secrets
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event, RLock
from typing import Literal

from app.config.logging.logger import log
from app.models.session.session_command_errors import SessionCommandConflictError

# 批准没等到决定的原因：原调用按它给模型说明（新输入取代 / 用户停止 / 其余一律「已取消」）。
CancelCause = Literal["user_message", "stopped", "deleted", "call_cancelled", "shutdown"]


@dataclass
class PendingTerminalApproval:
    request_id: str
    origin_session_id: int
    display_session_id: int
    run_id: int
    tool_call_id: str
    command: str
    shell: str
    cwd: str
    intent: str
    # 发起会话的名字：子 Agent 的批准显示在父会话页上，卡片写它的名字而不是 id。
    origin_title: str
    ready: Event = field(default_factory=Event)
    decision: Literal["approve", "reject", "cancelled"] | None = None
    cancel_cause: CancelCause | None = None

    def yielded(self, cause: CancelCause) -> PendingTerminalApproval:
        """没登记就让位了：以「已取消」收尾，原调用照常按取消原因说明，不进视图。"""
        self.decision = "cancelled"
        self.cancel_cause = cause
        self.ready.set()
        return self

    def view(self) -> dict[str, object]:
        return {
            "id": self.request_id,
            "originSessionId": self.origin_session_id,
            "toolCallId": self.tool_call_id,
            "command": self.command,
            "shell": self.shell,
            "cwd": self.cwd,
            "intent": self.intent,
            "originTitle": self.origin_title,
        }


class TerminalApprovalRegistry:
    """登记先于通知，锁外发布；snapshot 读取不反向持有 registry 锁。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._pending: dict[str, PendingTerminalApproval] = {}
        self._listener: Callable[[int], None] | None = None

    def set_listener(self, listener: Callable[[int], None] | None) -> None:
        self._listener = listener

    def request(
        self,
        *,
        origin_session_id: int,
        display_session_id: int,
        run_id: int,
        tool_call_id: str,
        command: str,
        shell: str,
        cwd: str,
        intent: str,
        origin_title: str,
    ) -> PendingTerminalApproval:
        """登记一次批准；调用方已持显示会话闸门并做完让位判定（``yielding.admit``）。"""
        request = PendingTerminalApproval(
            secrets.token_hex(16),
            origin_session_id,
            display_session_id,
            run_id,
            tool_call_id,
            command,
            shell,
            cwd,
            intent,
            origin_title,
        )
        with self._lock:
            self._pending[request.request_id] = request
        self._changed(request, "requested")
        return request

    def decide(
        self, display_session_id: int, request_id: str, decision: Literal["approve", "reject"]
    ) -> None:
        with self._lock:
            request = self._pending.get(request_id)
            if (
                request is None
                or request.display_session_id != display_session_id
                or request.decision is not None
            ):
                raise SessionCommandConflictError("TERMINAL_APPROVAL_EXPIRED", "这次终端批准已失效")
            request.decision = decision
            self._pending.pop(request_id)
            request.ready.set()
        self._changed(request, decision)

    def cancel(self, request: PendingTerminalApproval, cause: CancelCause) -> bool:
        with self._lock:
            if self._pending.pop(request.request_id, None) is None:
                return False
            request.decision = "cancelled"
            request.cancel_cause = cause
            request.ready.set()
        self._changed(request, "cancelled")
        return True

    def cancel_originated(self, session_id: int, cause: CancelCause) -> None:
        """撤销这个会话自己发起的批准（用户停止、删除）；子 Agent 显示在它页面上的不动。"""
        with self._lock:
            requests = [r for r in self._pending.values() if r.origin_session_id == session_id]
        for request in requests:
            self.cancel(request, cause)

    def cancel_call(self, session_id: int, tool_call_id: str) -> bool:
        with self._lock:
            requests = [
                r
                for r in self._pending.values()
                if r.origin_session_id == session_id and r.tool_call_id == tool_call_id
            ]
        cancelled = False
        for request in requests:
            cancelled = self.cancel(request, "call_cancelled") or cancelled
        return cancelled

    def close(self) -> None:
        with self._lock:
            requests = list(self._pending.values())
        for request in requests:
            self.cancel(request, "shutdown")

    def views(self, display_session_id: int) -> list[dict[str, object]]:
        with self._lock:
            return [
                r.view()
                for r in self._pending.values()
                if r.display_session_id == display_session_id
            ]

    def displayed(self, session_id: int) -> tuple[PendingTerminalApproval, ...]:
        """返回此页面仍在等待的原调用；调用方按共享 Session 闸门协调输入和批准。"""
        with self._lock:
            return tuple(r for r in self._pending.values() if r.display_session_id == session_id)

    def displayed_counts(self) -> Counter[int]:
        """各会话页上等待的批准数，供 Session 状态整表计入「等你操作」。"""
        with self._lock:
            return Counter(r.display_session_id for r in self._pending.values())

    def _changed(self, request: PendingTerminalApproval, outcome: str) -> None:
        log.info(
            "terminal_approval_changed",
            extra={
                "msg": "终端执行批准状态变化",
                "data": {
                    "request_id": request.request_id,
                    "session_id": request.origin_session_id,
                    "run_id": request.run_id,
                    "tool_call_id": request.tool_call_id,
                    "outcome": outcome,
                    "cancel_cause": request.cancel_cause,
                },
            },
        )
        if self._listener is not None:
            try:
                self._listener(request.display_session_id)
            except Exception:
                log.exception(
                    "terminal_approval_projection_failed",
                    extra={
                        "data": {
                            "session_id": request.display_session_id,
                            "request_id": request.request_id,
                        }
                    },
                )


terminal_approvals = TerminalApprovalRegistry()
