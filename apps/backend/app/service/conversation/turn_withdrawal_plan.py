"""撤回回合的范围：移出哪些行、撤销哪些调用、重新投递哪些 Session 级通知。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from app.core.workflow.message_queue.message_bodies.notice import NOTICE_LEVELS, NoticeKind
from app.models.message_context.message_metadata.user_message_metadata import (
    NoticeEntry,
    UserMessageMetadata,
)
from app.models.message_context.session_context import SessionContextRecord


@dataclass(frozen=True)
class WithdrawalPlan:
    """撤回的范围：移出哪些行、撤销哪些调用、重新投递哪些 Session 级通知。

    由纯函数 :meth:`plan_withdrawal` 算出，与执行分开：范围规则可以脱离数据库单测。
    """

    excluded_row_ids: frozenset[int]
    calls: frozenset[str]
    redeliver: tuple[NoticeEntry, ...]

    @classmethod
    def plan_withdrawal(cls, rows: list[SessionContextRecord], from_turn: int) -> WithdrawalPlan:
        """撤回 ``from_turn`` 及之后的本地回合：按回合号划范围，按调用归属保留更早调用的结果。

        继承行（``run_id`` 为空）不在范围内：撤回只针对本地的最新回合。范围取保留的行（``retained``：
        进上下文或在前端可见），不只看进上下文的行——被打断的流式草稿、预览时取消的占位从不进上下文却
        照常显示，只看上下文会把它们留在线程里，撤回后用户的话没了、半截回复还在。合成通知行整体移出，
        其中的 Session 级通知交给调用方重新投递（只关于被撤回调用的除外）；推导出的 Run 级与回合级通知
        随之丢弃，下一次驱动重新推导。
        """

        local = [r for r in rows if r.run_id is not None and r.retained]
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
                    # 只关于被撤回调用的通知随调用一起作废：重新投递会让模型读到它看不到的调用。
                    and not (notice.tool_call_ids and set(notice.tool_call_ids) <= calls)
                )
            excluded.append(cast(int, row.id))
        return cls(frozenset(excluded), calls, tuple(redeliver))
