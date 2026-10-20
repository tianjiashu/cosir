"""一次模型产出的工具批次；观察结算后整体丢弃。"""

from dataclasses import dataclass, field
from typing import Any, Literal

from openai.types.chat import ChatCompletionMessageFunctionToolCallParam

from app.models.user_decision import UserDecision


@dataclass(frozen=True)
class ValidToolCall:
    """有效调用的身份与前端投影状态，仅属于当前模型批次。"""

    name: str
    projection: Literal["projected", "banned"]


@dataclass
class ToolBatch:
    """把有效调用及其投影状态、工具执行与观察结算绑定到同一次模型请求。"""

    calls: list[ChatCompletionMessageFunctionToolCallParam] = field(default_factory=list)
    instruction: str = ""
    observations: list[dict[str, Any]] = field(default_factory=list)
    valid_tool_calls: dict[str, ValidToolCall] = field(default_factory=dict)
    approved_call: tuple[str, UserDecision] | None = None

    def was_projected(self, call_id: str) -> bool:
        call = self.valid_tool_calls.get(call_id)
        return call is not None and call.projection == "projected"

    def decision_for(self, call_id: str) -> UserDecision | None:
        if self.approved_call is not None and self.approved_call[0] == call_id:
            return self.approved_call[1]
        return None

    def merge_observations(self, incoming: list[dict[str, Any]]) -> None:
        """批准后重执行的目标观察覆盖占位，其它调用结果保持原顺序。"""
        positions = {item["tool_call_id"]: index for index, item in enumerate(self.observations)}
        for item in incoming:
            position = positions.get(item["tool_call_id"])
            if position is None:
                positions[item["tool_call_id"]] = len(self.observations)
                self.observations.append(item)
            else:
                self.observations[position] = item
