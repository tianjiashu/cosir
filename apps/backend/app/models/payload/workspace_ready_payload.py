"""Payload model for workspace_ready events."""

from typing import Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class WorkspaceReadyPayload(RuntimeEventPayload):
    """workspace 索引准备就绪事件 payload。

    ``action_taken`` 为就绪路径的归一化动作：init（首次建索引）或 sync（增量同步）。
    """

    workspace_path: str
    action_taken: Literal["init", "sync"]
    files_changed: int
    duration_ms: int
