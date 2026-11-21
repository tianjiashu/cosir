"""派生 Session 的进度摘要：它此刻在做什么、最近几步做了什么，供 ``session_list`` / ``session_read`` 给模型看。

为什么要单独一份投影：Run 状态只说「在跑」，派生方要决定等、追加指示还是停掉，需要知道它卡在哪一步。
对照 Claude Code 的 ``list_events``（读整段原文、工具调用只剩名字、工具结果也计入条数），这里按「步」
压缩：一条助手行连同它这批工具调用的结局算一步，输入行算一步，通知不算；不给推理原文与工具结果正文。

事实源只读持久化事实：context 行（``SessionContextCrud.list_recent_run_rows``）、Run 记录、外部队列，
以及挂起时它派生的 Session 的 Run 状态；不读 Transport 快照（那是前端投影缓存）。投影本身
（:meth:`SessionProgress.from_facts`）是纯函数，读取在 :meth:`SessionProgress.read` 一处。

时长按墙钟现算（工具在调用时刻给模型看，不进任何投影或持久化）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from app.config.constant import Constant
from app.core.context.context_store import ContextStore
from app.models import RunRecord, SessionRecord
from app.models.enums.run_status import ACTIVE_RUN_STATUSES, RunStatus
from app.models.message_metadata import ToolMessageMetadata, UserMessageMetadata
from app.models.session_context import SessionContextRecord
from app.models.session_kind import SessionType
from app.service.depends import (
    get_run_ledger,
    get_session_context_crud,
    get_session_message_queue,
    get_session_service,
)
from app.utils.datetime_utils import utc_now

# 每步最多读多少行：一步是一条助手行加它这批工具的结果行。一批很宽时更早的步会被挤出窗口，
# 只是少显示几步，不影响「此刻」——「此刻」只看最后几行。
_ROWS_PER_STEP = 6
_TEXT_LIMIT = 160
_ARGS_LIMIT = 100
_KIND_LABELS = {SessionType.DELEGATED: "delegated child agent", SessionType.AGENT_FORK: "fork"}
_CALL_STATUS = {"completed": "ok", "failed": "failed", "cancelled": "cancelled", "running": "running"}
_END_REASONS = {
    Constant.Run.RUN_END_REASON_USER_CANCELLED: "stopped by the user",
    Constant.Run.RUN_END_REASON_PARENT_CANCELLED: "stopped from upstream (you or your deriver)",
}


@dataclass(frozen=True)
class SessionProgress:
    """一个派生 Session 此刻的进度摘要；``status`` 为 ``None`` 表示还没开出第一个 Run。"""

    session_id: int
    name: str
    kind: str
    status: str | None
    elapsed: str | None
    now: str
    steps: tuple[str, ...]
    queued: int
    final_output: str | None
    end_reason: str | None
    # 最近几步之前还有没显示的步：模型据此知道看到的不是全部（不计总数，那要多读整段历史）。
    earlier_hidden: bool = False
    # 活动中的 Run 由用户输入开启：用户正在侧栏或工作台直接和它对话，这次 Run 是用户的、不会汇报给派生方。
    # 只标注、不拦停止：派生方据此自己判断别去打扰。
    user_driven: bool = False

    @classmethod
    def read(cls, session: SessionRecord, *, steps: int) -> SessionProgress:
        """读取 ``session`` 的进度事实并投影；``steps`` 为 0 时只读「此刻」需要的最后几行。"""
        run = get_session_service().require_current_run(session.id)
        window = max(steps, 1) * _ROWS_PER_STEP
        rows = get_session_context_crud().list_recent_run_rows(session.id, window)
        queued = len(get_session_message_queue().pending_nowait(session.id))
        waiting_on: list[str] = []
        if run is not None and run.status == RunStatus.SUSPENDED.value:
            waiting_on = cls._waiting_on(session.id)
        # 只对活动 Run 有意义：落定的 Run 不管是谁开的，此刻都没人在用它。
        user_driven = (
            run is not None
            and run.status in ACTIVE_RUN_STATUSES
            and ContextStore().run_opened_by_user(session.id, run.id)
        )
        return cls.from_facts(
            session, run, rows, queued=queued, waiting_on=waiting_on, steps=steps, now=utc_now(),
            window_full=len(rows) == window, user_driven=user_driven,
        )

    @classmethod
    def from_facts(
        cls,
        session: SessionRecord,
        run: RunRecord | None,
        rows: list[SessionContextRecord],
        *,
        queued: int,
        waiting_on: list[str],
        steps: int,
        now: datetime,
        window_full: bool = False,
        user_driven: bool = False,
    ) -> SessionProgress:
        """纯投影：``rows`` 是按序号升序的尾部行，``waiting_on`` 是挂起时它在等的东西（已写成短语）；
        ``window_full`` 表示尾部窗口读满了，窗口之前可能还有更早的步。"""
        results = cls._call_results(rows)
        running = run is not None and run.status == RunStatus.RUNNING.value
        last_assistant = cls._last_assistant_index(rows)
        entries: list[str] = []
        for index, row in enumerate(rows):
            entry = cls._step(row, results, live=running and index == last_assistant)
            if entry is not None:
                entries.append(entry)
        terminal = run is not None and run.status not in ACTIVE_RUN_STATUSES
        return cls(
            session_id=session.id,
            name=session.title,
            kind=_KIND_LABELS.get(session.session_type, session.session_type.value),
            status=None if run is None else run.status,
            elapsed=None if run is None else cls._elapsed(run, now),
            now=cls._now_line(run, rows, results, last_assistant, waiting_on),
            steps=tuple(entries[-steps:]) if steps > 0 else (),
            queued=queued,
            final_output=run.final_output if terminal and run is not None else None,
            end_reason=_END_REASONS.get(run.end_reason or "", run.end_reason)
            if terminal and run is not None
            else None,
            earlier_hidden=steps > 0 and (len(entries) > steps or window_full),
            user_driven=user_driven,
        )

    def summary_line(self, *, derived_by: int | None = None) -> str:
        """``session_list`` 的一行：id、名字、类型、状态与此刻；``derived_by`` 是不是调用方派生时它的派生方。"""
        state = "not started" if self.status is None else self.status
        if self.elapsed is not None:
            state = f"{state} {self.elapsed}"
        kind = self.kind if derived_by is None else f"{self.kind}, derived by #{derived_by}"
        line = f"#{self.session_id} {self.name} ({kind}) — {state} — {self.now}"
        if self.user_driven:
            line = f"{line}; the user is talking to it directly"
        return f"{line}; {self.queued} queued" if self.queued else line

    def render(self) -> str:
        """``session_read`` 的正文：状态、此刻、排队、最近几步与（落定后的）最终输出。"""
        state = "not started yet" if self.status is None else self.status
        if self.elapsed is not None:
            state = f"{state} ({self.elapsed})"
        lines = [
            f"session {self.session_id} {self.name} ({self.kind})",
            f"status: {state}",
            f"now: {self.now}",
        ]
        if self.user_driven:
            lines.append(
                "user: the user is working in it directly; this run is theirs and will not report to you, "
                "so do not wait for it"
            )
        if self.end_reason:
            lines.append(f"end reason: {self.end_reason}")
        if self.queued:
            lines.append(f"queued: {self.queued} message(s) not yet taken in")
        if self.steps:
            heading = f"last {len(self.steps)} steps, older ones not shown" if self.earlier_hidden else "all steps"
            lines.append(f"recent steps ({heading}; oldest first):")
            lines.extend(f"- {step}" for step in self.steps)
        if self.final_output:
            lines.extend(["final output:", self.final_output])
        if self._reports_later():
            lines.append(
                "next: it reports back to you when this run ends; to wait for it, use wait_messages "
                "instead of reading again"
            )
        return "\n".join(lines)

    def _reports_later(self) -> bool:
        """结果之后还会自己送到：Run 未开出或仍在活动，且不是用户的 Run。

        防轮询只靠这句固定提示、不记读取时间：模型反复读多半是怕错过结果，在它读完结果、决定下一步的
        那一刻告诉它「会主动汇报」比写在工具描述里管用。用户的 Run 不汇报（``derived_report``），那时
        提示「去等」会让它一直挂着，所以改由 ``user`` 一行说别等。
        """
        pending = self.status is None or self.status in ACTIVE_RUN_STATUSES
        return pending and not self.user_driven

    @staticmethod
    def _waiting_on(session_id: int) -> list[str]:
        """挂起时它在等的东西：仍在后台的命令与 Run 仍在活动的派生 Session。

        后台命令读未结算的后台占位（与 ``wait_messages`` 的等待判定同一份事实），不靠尾部窗口：
        很早启动的命令可能早已滑出窗口。
        """
        waits = [
            f"background command: {work.label}"
            for row in get_session_context_crud().list_unsettled_placeholders(session_id)
            if (work := cast(ToolMessageMetadata, row.metadata).background) is not None
        ]
        ledger = get_run_ledger()
        for derived in get_session_service().list_derived_sessions(session_id):
            if derived.current_run_id is None:
                continue
            if ledger.get_run(derived.current_run_id).status in ACTIVE_RUN_STATUSES:
                waits.append(f"derived session: {derived.title}")
        return waits

    @staticmethod
    def _call_results(rows: list[SessionContextRecord]) -> dict[str, ToolMessageMetadata]:
        return {
            str(row.message.get("tool_call_id")): cast(ToolMessageMetadata, row.metadata)
            for row in rows
            if row.message["role"] == "tool"
        }

    @staticmethod
    def _last_assistant_index(rows: list[SessionContextRecord]) -> int | None:
        for index in range(len(rows) - 1, -1, -1):
            if rows[index].message["role"] == "assistant":
                return index
        return None

    @classmethod
    def _step(
        cls, row: SessionContextRecord, results: dict[str, ToolMessageMetadata], *, live: bool
    ) -> str | None:
        """一行投影成一步；工具结果行折进发起它的助手行，通知与工具图片不算一步。"""
        role = row.message["role"]
        if role == "assistant":
            message = cast(dict[str, Any], row.message)
            parts: list[str] = []
            text = cls._clip(message.get("content"), _TEXT_LIMIT)
            if not row.include_in_context:
                parts.append("writing a reply…" if live else "interrupted output")
                if text:
                    parts.append(f'"{text}"')
            elif text:
                parts.append(f'said "{text}"')
            for call in message.get("tool_calls") or []:
                parts.append(cls._call_line(call, results.get(call["id"]), live=live))
            return "; ".join(parts) if parts else None
        if role == "user":
            metadata = cast(UserMessageMetadata, row.metadata)
            text = cls._clip(row.message.get("content"), _TEXT_LIMIT)
            if metadata.kind == "user_input":
                return f'user said "{text}"'
            if metadata.kind == "agent_message" and metadata.source is not None:
                return f'message from {metadata.source.name}: "{text}"'
            if metadata.kind == "background_result":
                return "a background command result arrived"
        return None

    @classmethod
    def _call_line(cls, call: dict[str, Any], result: ToolMessageMetadata | None, *, live: bool) -> str:
        function = call["function"]
        target = f"{function['name']}({cls._args_summary(function.get('arguments'))})"
        if result is None:
            return f"{target} → {'running' if live else 'no result'}"
        if result.status == "pending":
            return f"{target} → {'running in background' if result.background else 'waiting for a decision'}"
        return f"{target} → {_CALL_STATUS.get(result.status, result.status)}"

    @classmethod
    def _now_line(
        cls,
        run: RunRecord | None,
        rows: list[SessionContextRecord],
        results: dict[str, ToolMessageMetadata],
        last_assistant: int | None,
        waiting_on: list[str],
    ) -> str:
        if run is None:
            return "waiting for its first run to start"
        if run.status == RunStatus.SUSPENDED.value:
            return f"paused, waiting for {', '.join(waiting_on)}" if waiting_on else "paused, waiting for messages"
        if run.status != RunStatus.RUNNING.value:
            return "idle (run ended)"
        # 最后一条助手行之后只跟着它这批工具的结果行时，它就是当前这一步；之后又并入了输入，就是在等
        # 下一次模型请求。
        current = last_assistant is not None and all(
            row.message["role"] == "tool" for row in rows[last_assistant + 1 :]
        )
        if not current:
            return "waiting for the model"
        row = rows[cast(int, last_assistant)]
        if not row.include_in_context:
            return "writing a reply"
        calls = cast(dict[str, Any], row.message).get("tool_calls") or []
        open_calls = [call["function"]["name"] for call in calls if call["id"] not in results]
        if open_calls:
            return f"running tools: {', '.join(open_calls)}"
        return "waiting for the model"

    @classmethod
    def _args_summary(cls, arguments: object) -> str:
        """参数摘要：工具自己的 ``intent`` 优先（终端命令、页面脚本），否则紧凑 JSON 截断。"""
        if not isinstance(arguments, str):
            return ""
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return cls._clip(arguments, _ARGS_LIMIT)
        if isinstance(parsed, dict) and isinstance(parsed.get("intent"), str):
            return cls._clip(parsed["intent"], _ARGS_LIMIT)
        return cls._clip(json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), _ARGS_LIMIT)

    @staticmethod
    def _clip(value: object, limit: int) -> str:
        if not isinstance(value, str):
            return ""
        text = " ".join(value.split())
        return text if len(text) <= limit else f"{text[: limit - 1]}…"

    @staticmethod
    def _elapsed(run: RunRecord, now: datetime) -> str:
        """活动 Run 说已运行多久，落定的说用了多久（到最后一次状态更新）。"""
        end = now if run.status in ACTIVE_RUN_STATUSES else run.updated_at
        seconds = max(int((end - run.created_at).total_seconds()), 0)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        text = f"{hours}h{minutes}m" if hours else f"{minutes}m{seconds}s" if minutes else f"{seconds}s"
        return f"for {text}" if run.status in ACTIVE_RUN_STATUSES else f"took {text}"
