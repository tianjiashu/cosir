"""一次模型产出的工具批次：随 ``ReactExecutionState`` 在 model → tools → observe → prepare_input 间交接。"""

from typing import Any, Literal

from openai.types.chat import ChatCompletionMessageFunctionToolCallParam
from pydantic import BaseModel, ConfigDict, Field


class ValidToolCall(BaseModel):
    """有效调用的身份与前端投影状态，仅属于当前模型批次。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    projection: Literal["projected", "banned"]


class ToolBatch(BaseModel):
    """把有效调用及其投影状态、工具执行与观察结算绑定到同一次模型请求。

    model 节点每次请求新建并写入调用与投影记账，tools 节点写入观察摘要，observe 节点据此落库与
    投影终态，prepare_input 读挂起请求后清空。它随执行状态整体落库，但新驱动总从空批次开始
    （见 ``ReactExecutionState.for_new_drive``），不据此重放工具。
    """

    model_config = ConfigDict(extra="forbid")

    calls: list[ChatCompletionMessageFunctionToolCallParam] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    valid_tool_calls: dict[str, ValidToolCall] = Field(default_factory=dict)
    # 用户对 HITL 调用作出决定后，prepare_input 绕过模型组出的单调用批次所携带的决定（``DecisionBody``
    # 的 JSON 形态；本层不依赖 core 的消息类型）。tools 节点把它交给 handler，observe 节点据此把
    # 结果追加为消息、只更新占位的展示状态，而不是写 tool 消息。
    decision: dict[str, Any] | None = None

    def was_projected(self, call_id: str) -> bool:
        call = self.valid_tool_calls.get(call_id)
        return call is not None and call.projection == "projected"

    def suspends(self) -> bool:
        """本批是否有调用要求挂起（见 ``ToolObservation.suspend``）。"""
        return any(item.get("suspend") for item in self.observations)
