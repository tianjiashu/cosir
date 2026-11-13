"""撤回回合的范围：移出哪些行、撤销哪些调用、重新投递哪些 Session 级通知。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from app.core.workflow.message_queue.message_bodies.notice import NOTICE_LEVELS, NoticeKind
from app.models.session_context import SessionContextRecord
from app.models.message_metadata import NoticeEntry, UserMessageMetadata


@dataclass(frozen=True)
class WithdrawalPlan:
    """撤回的范围：移出哪些行、撤销哪些调用、重新投递哪些 Session 级通知。

    由纯函数 :meth:`plan_withdrawal` 算出，与执行分开：范围规则可以脱离数据库单测。
    """

    excluded_row_ids: frozenset[int]
    calls: frozenset[str]
    redeliver: tuple[NoticeEntry, ...]

    @classmethod
    def plan_withdrawal(
        cls, rows: list[SessionContextRecord], from_turn: int
    ) -> WithdrawalPlan:
        """撤回 ``from_turn`` 及之后的本地回合：按回合号划范围，按调用归属保留更早调用的结果。

        继承行（``run_id`` 为空）不在范围内：撤回只针对本地的最新回合。合成通知行整体移出，其中的
        Session 级通知交给调用方重新投递；推导出的 Run 级与回合级通知随之丢弃，下一次驱动重新推导。
        """

        local = [r for r in rows if r.run_id is not None and r.include_in_context]
        earlier_calls = {
            call["id"]
            for r in local
            if r.turn < from_turn and r.message["role"] == "assistant"
            for call in r.message.get("tool_calls") or ()
        }
        withdrawn = [r for r in local if r.turn >= from_turn]
        calls = frozenset(
            call["id"]
            for r in withdrawn
            if r.message["role"] == "assistant"
            for call in r.message.get("tool_calls") or ()
        )
        excluded: list[int] = []
        redeliver: list[NoticeEntry] = []
        for row in withdrawn:
            metadata = row.metadata
            if isinstance(metadata, UserMessageMetadata):
                if metadata.tool_call_id in earlier_calls:
                    continue
                redeliver.extend(
                    notice
                    for notice in metadata.notices
                    if NOTICE_LEVELS[cast(NoticeKind, notice.notice_kind)] == "session"
                )
            excluded.append(cast(int, row.id))
        return cls(frozenset(excluded), calls, tuple(redeliver))
