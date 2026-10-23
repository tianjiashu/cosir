"""一次模型产出的工具批次；观察结算后整体丢弃。"""

from dataclasses import dataclass, field
from typing import Any, Literal

from openai.types.chat import ChatCompletionMessageFunctionToolCallParam


@dataclass(frozen=True)
class ValidToolCall:
    """有效调用的身份与前端投影状态，仅属于当前模型批次。"""

    name: str
    projection: Literal["projected", "banned"]


@dataclass
class ToolBatch:
    """把有效调用及其投影状态、工具执行与观察结算绑定到同一次模型请求。"""

    calls: list[ChatCompletionMessageFunctionToolCallParam] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    valid_tool_calls: dict[str, ValidToolCall] = field(default_factory=dict)

    def was_projected(self, call_id: str) -> bool:
        call = self.valid_tool_calls.get(call_id)
        return call is not None and call.projection == "projected"

    def suspends(self) -> bool:
        """本批是否有调用要求挂起（见 ``ToolObservation.suspend``）。"""
        return any(item.get("suspend") for item in self.observations)
