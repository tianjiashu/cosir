"""消息 part 的文本内容与阶段事件。

本模块只承载「一条消息内 text / reasoning part 的内容追加与阶段收口」这一单一职责。

不负责：Run 骨架的建立（见 ``run_event.RunInitializedEvent``）、工具调用 part（见
``tool_call_event``）、消息终态（见 ``run_event.RunStatusChangedEvent``）。
"""

from typing import Literal

from pydantic import Field

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope

AssistantTextPartKind = Literal["text", "reasoning", "tool_call"]


class AssistantTextDeltaEvent(WorkflowEventEnvelope):
    """模型输出的一段增量文本已经产生。

    事实语义：模型流式产出了一个 chunk 中的文本或 reasoning 内容。text 与 reasoning 形状完全
    相同，合并为一个类型由 ``part`` 区分，避免为两种通道各维护一套事件与投影分支。

    Attributes:
        part: 目标 part 类型，``"text"`` 为正文、``"reasoning"`` 为思考内容。
        delta: 非空增量文本。空增量不构成事实，不得发出。

    异常:
        pydantic.ValidationError: ``part`` 取值非法、``delta`` 为空串，或出现未声明字段时抛出。
    """

    type: Literal["assistant_text_delta"] = "assistant_text_delta"
    part: AssistantTextPartKind
    delta: str = Field(min_length=1)


class AssistantPartClosedEvent(WorkflowEventEnvelope):
    """一个 assistant text / reasoning part 已停止追加。

    单独成事件而不是由消费方隐式推断：只有生产者知道一段内容何时算写完（模型可能在同一 part
    内多次停顿，也可能被取消打断），推断会产生「永远不 completed」的悬挂 part。

    Attributes:
        part: 要收口的 part 类型，取值同 ``AssistantTextDeltaEvent.part``。

    异常:
        pydantic.ValidationError: ``part`` 取值非法，或出现未声明字段时抛出。
    """

    type: Literal["assistant_part_closed"] = "assistant_part_closed"
    part: AssistantTextPartKind
