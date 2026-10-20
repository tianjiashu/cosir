"""Task 级 Agent context 值对象。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import uuid4

from openai.types.chat import ChatCompletionMessageParam

from app.core.llm_provider.message_codec import validate_message
from app.models.message_metadata import (
    EmptyMessageMetadata,
    MessageMetadata,
    empty_message_metadata,
    parse_message_metadata,
)
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel


@dataclass(frozen=True)
class ConversationTaskContextRecord:
    task_id: int
    run_id: int | None
    message: ChatCompletionMessageParam
    include_in_context: bool
    sequence: int
    message_id: str = field(default_factory=lambda: str(uuid4()))
    metadata: MessageMetadata = field(default_factory=EmptyMessageMetadata)
    # 与 ``include_in_context`` 独立：消息可供模型使用但不对前端投影。
    project_to_transport: bool = True
    id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        """默认空数据只用于无附属信息的消息；显式 metadata 必须匹配正文角色。"""
        if type(self.metadata) is EmptyMessageMetadata:
            object.__setattr__(self, "metadata", empty_message_metadata(self.message["role"]))
        else:
            object.__setattr__(
                self, "metadata", parse_message_metadata(self.message["role"], self.metadata)
            )

    @classmethod
    def _from_model(cls, model: ConversationTaskContextModel) -> ConversationTaskContextRecord:
        """读取边界校验 SDK 正文及对应角色 metadata；旧字段和错误角色结构直接报错。"""
        message_doc = json.loads(model.message_json)
        return cls(
            id=model.id,
            task_id=model.task_id,
            run_id=model.run_id,
            message=validate_message(message_doc),
            message_id=model.message_id,
            metadata=parse_message_metadata(message_doc["role"], json.loads(model.metadata_json)),
            include_in_context=model.include_in_context,
            project_to_transport=model.project_to_transport,
            sequence=model.sequence,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    def _to_model(self) -> ConversationTaskContextModel:
        """将记录映射为持久化模型实例。

        本方法只负责字段映射，不生成主键或唯一约束相关派生值；``sequence`` 由调用方在落盘前按
        任务维度统一分配以保证唯一性。创建和更新时间是持久化层管理的只读字段，不从值对象写回。

        返回:
            含 task_id、run_id、消息 JSON、模型纳入标记、Transport 投影标记与排序的模型实例。

        异常:
            TypeError: SDK 消息包含无法 JSON 序列化的值。
            ValueError: metadata 无法序列化为 JSON。

        副作用:
            无副作用；纯映射。
        """

        return ConversationTaskContextModel(
            id=self.id,
            task_id=self.task_id,
            run_id=self.run_id,
            message_json=json.dumps(self.message, ensure_ascii=False, allow_nan=False),
            message_id=self.message_id,
            metadata_json=self.metadata.model_dump_json(),
            include_in_context=self.include_in_context,
            project_to_transport=self.project_to_transport,
            sequence=self.sequence,
        )
