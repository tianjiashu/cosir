"""Task 级 LangChain Agent context 值对象。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from langchain_core.messages import BaseMessage, ToolMessage, _message_from_dict, message_to_dict

from app.models.json_helpers import (
    TransportMetadata,
    deserialize_transport_metadata,
    empty_transport_metadata,
    serialize_transport_metadata,
)
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel


@dataclass(frozen=True)
class ConversationTaskContextRecord:
    task_id: int
    run_id: int | None
    message: BaseMessage
    include_in_context: bool
    sequence: int
    transport_metadata: TransportMetadata = field(default_factory=empty_transport_metadata)
    id: int | None = None

    @classmethod
    def _from_model(cls, model: ConversationTaskContextModel) -> ConversationTaskContextRecord:
        """从持久化模型重建一条上下文记录。

        参数:
            model: ``conversation_task_contexts`` 行实例，列已含单条消息的全部字段。

        返回:
            含 row id、task_id、run_id、反序列化消息、Transport metadata、schema version、
            纳入标记与排序的记录。

        异常:
            json.JSONDecodeError: ``message_json`` 不是合法 JSON。
            KeyError: ``message_json`` 反序列化结果不是 ``message_to_dict`` 约定结构。
            TypeError: ``transport_metadata_json`` 不是 JSON 文本或 metadata 含非法值。
            ValueError: ``transport_metadata_json`` 不符合 typed metadata 契约。

        副作用:
            无副作用；纯映射。
        """

        message_doc = json.loads(model.message_json)
        return cls(
            id=model.id,
            task_id=model.task_id,
            run_id=model.run_id,
            message=_message_from_dict(message_doc),
            include_in_context=model.include_in_context,
            sequence=model.sequence,
            transport_metadata=deserialize_transport_metadata(model.transport_metadata_json),
        )

    def _to_model(self) -> ConversationTaskContextModel:
        """将记录映射为持久化模型实例。

        本方法只负责字段映射，不生成 ``task_id`` 之外的主键或唯一约束相关派生值；
        ``sequence`` 由调用方在落盘前按任务维度统一分配以保证唯一性。

        返回:
            含 task_id、run_id、消息 JSON、纳入标记与排序的模型实例。

        异常:
            TypeError: ``message`` 无法被 ``message_to_dict`` 序列化。
            TypeError: ``transport_metadata`` 无法编码为 typed JSON metadata。

        副作用:
            无副作用；纯映射。
        """

        return ConversationTaskContextModel(
            id=self.id,
            task_id=self.task_id,
            run_id=self.run_id,
            tool_call_id=(
                self.message.tool_call_id if isinstance(self.message, ToolMessage) else None
            ),
            message_json=json.dumps(message_to_dict(self.message), ensure_ascii=False),
            transport_metadata_json=serialize_transport_metadata(self.transport_metadata),
            include_in_context=self.include_in_context,
            sequence=self.sequence,
        )
