"""ReAct workflow 内部传递模型输出增量的中性 stream 契约。"""

from typing import Literal, TypedDict

ModelOutputPart = Literal["text", "reasoning"]


class ModelOutputDelta(TypedDict):
    """一次尚未持久化为完整消息的模型输出增量。"""

    type: Literal["model_output_delta"]
    part: ModelOutputPart
    text: str
