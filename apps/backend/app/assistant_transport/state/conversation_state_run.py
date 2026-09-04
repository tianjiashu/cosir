"""Conversation Transport 的 run 运行状态契约。"""

from typing import TypedDict


class ConversationStateRun(TypedDict):
    """当前 Task 的 UI 运行状态。"""

    runId: int | None
    status: str
