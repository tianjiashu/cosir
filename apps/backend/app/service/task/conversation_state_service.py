"""Project canonical conversation facts into Assistant Transport state."""

from __future__ import annotations

import json
from typing import cast

from app.assistant_transport.state.conversation_state_part import ConversationStatePart
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateMessage,
    ConversationStateSnapshot,
)
from app.models.conversation_tool_call_record import ConversationToolCallRecord
from app.assistant_transport.service.conversation_snapshot_service import ConversationTaskSnapshotService
from app.storage.crud.conversation_message_crud import ConversationMessageCrud
from app.storage.crud.conversation_message_part_crud import ConversationMessagePartCrud
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.conversation_tool_call_crud import ConversationToolCallCrud
from app.storage.crud.task_crud import TaskCrud


class ConversationStateService:
    """只读读取 canonical facts 并投影为中性 state 快照。"""

    def __init__(self) -> None:
        """绑定会话消息、part、工具调用和运行事实读取端口。"""
        self._messages = ConversationMessageCrud()
        self._parts = ConversationMessagePartCrud()
        self._tool_calls = ConversationToolCallCrud()
        self._turns = ConversationRunCrud()
        self._tasks = TaskCrud()
        self._snapshots = ConversationTaskSnapshotService()

    def build_messages(
        self, task_id: int, exclude_run_id: int | None = None
    ) -> list[ConversationStateMessage]:
        """按 canonical message sequence 投影消息及其结构化 parts。"""
        projected: list[ConversationStateMessage] = []
        tool_calls = self._tool_calls.list_by_task(task_id)
        for record in self._messages.list_by_task(task_id):
            if exclude_run_id is not None and record.run_id == exclude_run_id:
                continue
            projected_parts: list[ConversationStatePart] = []
            calls_for_message = [call for call in tool_calls if call.message_id == record.id]
            calls_by_part = {
                call.part_id: call for call in calls_for_message if call.part_id is not None
            }
            projected_call_ids: set[str] = set()
            for part in self._parts.list_by_message(record.id):
                call = calls_by_part.get(part.id)
                if call is not None:
                    projected_parts.append(self._tool_call_part(call))
                    projected_call_ids.add(call.tool_call_id)
                    continue
                item: dict[str, object] = {"type": part.part_type, "status": part.status}
                if part.text is not None:
                    item["text"] = part.text
                if part.data_json:
                    try:
                        value = json.loads(part.data_json)
                    except json.JSONDecodeError:
                        value = {"raw": part.data_json}
                    if isinstance(value, dict):
                        item.update(value)
                projected_parts.append(cast(ConversationStatePart, item))
            # 兼容历史数据：尚未关联 part 的工具调用仍可显示，但新写入必须有 part_id。
            for call in calls_for_message:
                if call.tool_call_id not in projected_call_ids:
                    projected_parts.append(self._tool_call_part(call))
            if not projected_parts:
                projected_parts.append({"type": "text", "text": "", "status": record.status})
            projected.append(
                {
                    "id": f"message-{record.id}",
                    "runId": record.run_id,
                    "role": record.role,
                    "status": record.status,
                    "endReason": record.end_reason,
                    "createdAt": record.created_at.isoformat(),
                    "parts": projected_parts,
                }
            )
        return projected

    @staticmethod
    def _tool_call_part(call: ConversationToolCallRecord) -> ConversationStatePart:
        """Project one canonical tool-call fact without exposing storage types."""

        item: dict[str, object] = {
            "type": "tool-call",
            "toolCallId": call.tool_call_id,
            "toolName": call.tool_name,
            "status": call.status,
        }
        try:
            item["args"] = json.loads(call.args_json)
        except json.JSONDecodeError:
            item["args"] = {"raw": call.args_json}
        if call.status == "completed":
            item["result"] = None
        if call.result_json is not None:
            try:
                item["result"] = json.loads(call.result_json)
            except json.JSONDecodeError:
                item["result"] = call.result_json
        if call.error_text is not None:
            item["error"] = call.error_text
            item["isError"] = call.status == "failed"
        elif call.status == "failed":
            item["isError"] = True
        return cast(ConversationStatePart, item)

    def build_initial_history_state(self, task_id: int) -> ConversationStateSnapshot:
        """读取当前 Task 快照；缺失时只初始化为空快照。"""
        persisted = self._snapshots.load(task_id)
        if persisted is not None:
            return persisted
        return self._snapshots.read_or_initialize(
            task_id,
            {
                "messages": [],
                "run": {"runId": None, "status": "idle"},
                "error": None,
            },
        )

    def ensure_task_snapshot(self, task_id: int) -> ConversationStateSnapshot:
        """确保任务存在快照；仅在一次性兼容初始化时从旧事实表投影。"""

        self._tasks.get(task_id)
        persisted = self._snapshots.load(task_id)
        if persisted is not None:
            return persisted
        turns = self._turns.list_by_task(task_id)
        latest = turns[-1] if turns else None
        return self._snapshots.read_or_initialize(
            task_id,
            {
                "messages": self.build_messages(task_id),
                "run": {
                    "runId": latest.id if latest is not None else None,
                    "status": latest.status if latest is not None else "idle",
                },
                "error": None,
            },
        )

    def build_initial_state(
        self, task_id: int, current_run_id: int, current_text: str
    ) -> ConversationStateSnapshot:
        """构造已创建运行的 canonical 快照；不采信客户端文本回传。"""
        del current_text
        persisted = self._snapshots.load(task_id)
        if persisted is None or persisted["run"]["runId"] != current_run_id:
            raise KeyError(current_run_id)
        return persisted

    def build_run_state(self, task_id: int, run_id: int) -> ConversationStateSnapshot:
        """构造指定 run 的 canonical 快照，不受 task 后续 run 影响。

        参数:
            task_id: task/Conversation Thread 标识。
            run_id: 要订阅的 Conversation Run 标识。

        返回:
            包含指定 run 状态与当前 canonical messages 的 Transport 快照。

        异常:
            KeyError: run 不存在或不属于 task。
            sqlalchemy.exc.SQLAlchemyError: 读取事实失败。

        副作用:
            只读访问 runs、messages、parts、tool calls 与 task 事实。
        """
        persisted = self._snapshots.load(task_id)
        if persisted is None or persisted["run"]["runId"] != run_id:
            raise KeyError(run_id)
        return persisted
