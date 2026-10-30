"""完整快照的历史窗口：只把最近一段消息发给前端，更早的按页取。

后端工作副本始终保存完整历史，投影与增量协议都不感知窗口；这里只在完整快照离开后端（HTTP 读取、
attach 首帧、重新发布）时裁剪，以及按位置切出更早的一页。窗口与页的起点都往前对齐到一条用户
消息：一轮对话不被切开，窗口里总有可编辑、可恢复的最新用户消息。
"""

from __future__ import annotations

from typing import cast

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.constant import Constant


class HistoryPage(TypedDict):
    """``messages`` 是完整历史里 ``[messageOffset, end)`` 的一段。"""

    messages: list[ConversationStateMessage]
    messageOffset: int


def window_snapshot(state: ConversationStateSnapshot) -> ConversationStateSnapshot:
    """返回只带最近一段消息的浅拷贝；``state`` 必须是完整历史（``messageOffset`` 为 0）。"""

    messages = state["messages"]
    start = _turn_start(messages, len(messages) - Constant.Transport.HISTORY_WINDOW_MESSAGES)
    if start == 0:
        return state
    return cast(
        ConversationStateSnapshot,
        {**state, "messages": messages[start:], "messageOffset": start},
    )


def history_page(messages: list[ConversationStateMessage], end: int) -> HistoryPage:
    """完整历史中 ``end`` 之前的一页。

    异常:
        ValueError: ``end`` 超出当前历史——客户端的窗口来自已被替换的完整快照（如编辑重跑），
            页位置已经失效。
    """

    if end > len(messages):
        raise ValueError("history page end is beyond the current history")
    start = _turn_start(messages, end - Constant.Transport.HISTORY_WINDOW_MESSAGES)
    return HistoryPage(messages=messages[start:end], messageOffset=start)


def _turn_start(messages: list[ConversationStateMessage], start: int) -> int:
    """把起点往前移到最近一条用户消息；前面没有用户消息时保持原起点。"""

    if start <= 0:
        return 0
    for index in range(start, -1, -1):
        if messages[index]["role"] == "user":
            return index
    return start
