"""对话正文增量及收口事实；推理仅以 ModelActivityEvent 表示阶段。"""

from typing import Literal

from pydantic import Field

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope


class AssistantTextDeltaEvent(WorkflowEventEnvelope):
    """非空正文增量；推理原文只保存在 canonical context。"""

    type: Literal["assistant_text_delta"] = "assistant_text_delta"
    source_sequence: int
    # 这次响应所在的回合：投影按它找到（或建出）这一回合的回复。
    turn: int
    part: Literal["text"] = "text"
    delta: str = Field(min_length=1)


class AssistantPartClosedEvent(WorkflowEventEnvelope):
    """生产者确认当前正文段停止追加；消费者不从停顿推断完成。"""

    type: Literal["assistant_part_closed"] = "assistant_part_closed"
    source_sequence: int
    turn: int
    part: Literal["text"] = "text"
