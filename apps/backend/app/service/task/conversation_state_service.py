"""Project canonical conversation facts into Assistant Transport state."""

from __future__ import annotations

import json

from app.models.conversation_tool_call_record import ConversationToolCallRecord
from app.models.enums.turn_status import TurnStatus
from app.service.task.conversation_state_snapshot import (
    ConversationStateMessage,
    ConversationStatePart,
    ConversationStateSnapshot,
)
from app.storage.crud.conversation_head_crud import ConversationHeadCrud
from app.storage.crud.conversation_message_crud import ConversationMessageCrud
from app.storage.crud.conversation_message_part_crud import ConversationMessagePartCrud
from app.storage.crud.conversation_tool_call_crud import ConversationToolCallCrud
from app.storage.crud.turn_crud import TurnCrud


class ConversationStateService:
    """只读读取 canonical facts 并投影为中性 state 快照。"""

    def __init__(self) -> None:
        """绑定会话消息、part、工具调用和 revision 读取端口。"""
        self._messages = ConversationMessageCrud()
        self._parts = ConversationMessagePartCrud()
        self._tool_calls = ConversationToolCallCrud()
        self._turns = TurnCrud()
        self._heads = ConversationHeadCrud()

    def build_messages(
        self, task_id: int, exclude_turn_id: int | None = None
    ) -> list[ConversationStateMessage]:
        """按 canonical message sequence 投影消息及其结构化 parts。"""
        projected: list[ConversationStateMessage] = []
        tool_calls = self._tool_calls.list_by_task(task_id)
        for record in self._messages.list_by_task(task_id):
            if exclude_turn_id is not None and record.turn_id == exclude_turn_id:
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
                item: ConversationStatePart = {"type": part.part_type, "status": part.status}
                if part.text is not None:
                    item["text"] = part.text
                if part.data_json:
                    try:
                        value = json.loads(part.data_json)
                    except json.JSONDecodeError:
                        value = {"raw": part.data_json}
                    if isinstance(value, dict):
                        item.update(value)
                projected_parts.append(item)
            # 兼容历史数据：尚未关联 part 的工具调用仍可显示，但新写入必须有 part_id。
            for call in calls_for_message:
                if call.tool_call_id not in projected_call_ids:
                    projected_parts.append(self._tool_call_part(call))
            if not projected_parts:
                projected_parts.append({"type": "text", "text": "", "status": record.status})
            projected.append(
                {
                    "id": f"message-{record.id}",
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

        item: ConversationStatePart = {
            "type": "tool-call",
            "toolCallId": call.tool_call_id,
            "toolName": call.tool_name,
            "status": call.status,
        }
        try:
            item["args"] = json.loads(call.args_json)
        except json.JSONDecodeError:
            item["args"] = {"raw": call.args_json}
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
        return item

    def build_initial_history_state(self, task_id: int) -> ConversationStateSnapshot:
        """构造当前 task 的 canonical 历史快照。"""
        turns = self._turns.list_by_task(task_id)
        latest = turns[-1] if turns else None
        run_id = latest.id if latest is not None else None
        run_status = latest.status if latest is not None else "idle"
        return {
            "messages": self.build_messages(task_id),
            "run": {"runId": run_id, "status": run_status},
            "revision": self._revision(task_id),
            "error": None,
        }

    def build_initial_state(
        self, task_id: int, current_turn_id: int, current_text: str
    ) -> ConversationStateSnapshot:
        """构造已创建运行的 canonical 快照；不采信客户端文本回传。"""
        del current_text
        if not any(
            message.turn_id == current_turn_id for message in self._messages.list_by_task(task_id)
        ):
            raise KeyError(current_turn_id)
        return {
            "messages": self.build_messages(task_id),
            "run": {"runId": current_turn_id, "status": TurnStatus.PENDING.value},
            "revision": self._revision(task_id),
            "error": None,
        }

    def build_run_state(self, task_id: int, run_id: int) -> ConversationStateSnapshot:
        """构造指定 run 的 canonical 快照，不受 task 后续 run 影响。

        参数:
            task_id: task/Conversation Thread 标识。
            run_id: 要订阅的 Conversation Run 标识。

        返回:
            包含指定 run 状态、当前 canonical messages 与 revision 的 Transport 快照。

        异常:
            KeyError: run 不存在或不属于 task。
            sqlalchemy.exc.SQLAlchemyError: 读取事实失败。

        副作用:
            只读访问 turns、messages、parts、tool calls 与 conversation head。
        """
        run = next((item for item in self._turns.list_by_task(task_id) if item.id == run_id), None)
        if run is None:
            raise KeyError(run_id)
        return {
            "messages": self.build_messages(task_id),
            "run": {"runId": run.id, "status": run.status},
            "revision": self._revision(task_id),
            "error": None,
        }

    def _revision(self, task_id: int) -> int:
        """读取 task 的已提交 conversation revision。"""
        head = self._heads.get_by_task(task_id)
        return 0 if head is None else head.revision
