"""Task 级 Agent context 值对象。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field

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
    # 所属回合的 Task 内序号（系统提示词为 0），见 ``ConversationTaskContextModel.turn``。
    turn: int
    # 只有用户消息有身份（message_queue 行 id）；其余角色为 None。
    message_id: int | None = None
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

    @property
    def retained(self) -> bool:
        """本地已移出行不复制；无 Run 的继承副本不可编辑，隐藏也不等于移出。

        Agent fork 会隐藏 include=False 的中断输出，因而两标志都可能为 False；这些
        无本地 Run 的副本仍保留供手动再分叉展示。固定系统提示词的序号为 0。
        """
        return (
            self.include_in_context or self.project_to_transport
            or (self.run_id is None and self.sequence > 0)
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
            turn=model.turn,
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
            turn=self.turn,
        )


def turn_withdrawn(turn_rows: Iterable[ConversationTaskContextRecord]) -> bool:
    """一个回合是否已被撤回：看开启它的那一行（回合内序号最小）是否还保留。

    撤回从开启回合的那一行起移出，但更早调用在这一回合才并入的结果会留下、仍带这个回合号，所以
    不能看「还有没有行保留着」。看 ``retained`` 而不是 ``include_in_context``：开启回合的也可能是
    从不进上下文的流式草稿（没有输入开头的 Run 第一行）。恢复、fork 定位与冷重建的「可恢复」
    共用这一判定。
    """
    opener = min(turn_rows, key=lambda row: row.sequence, default=None)
    return opener is not None and not opener.retained


def run_last_turn_withdrawn(run_rows: Iterable[ConversationTaskContextRecord]) -> bool:
    """一个 Run 的最后一个回合是否已被撤回（没写过行的 Run 不算）。

    ``run_rows`` 是这个 Run 的全部行（含已移出的）。

    撤回之后这个 Run 没有可续的回合：恢复会把新输出接在已撤回的回合上，「用户已停止」也说的是模型已
    看不到的回合。
    """
    rows = list(run_rows)
    if not rows:
        return False
    last = max(row.turn for row in rows)
    return turn_withdrawn(row for row in rows if row.turn == last)
