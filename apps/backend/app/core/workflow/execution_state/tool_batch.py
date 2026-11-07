"""一次模型产出的工具批次：在模型、执行、观察和输入准备节点间交接。"""

from typing import Any

from openai.types.chat import ChatCompletionMessageFunctionToolCallParam
from pydantic import BaseModel, ConfigDict, Field

from app.core.tool.schemas import ToolObservation


class ToolBatch(BaseModel):
    """把模型调用、工具执行与观察结算绑定到同一次模型请求。

    model 节点每次请求新建并写入调用，tools 节点写入观察，observe 节点据此落库与
    投影终态，prepare_input 读挂起请求后清空。它随执行状态整体落库，但新驱动总从空批次开始
    （见 ``ReactExecutionState.for_new_drive``），不据此重放工具。
    """

    model_config = ConfigDict(extra="forbid")

    calls: list[ChatCompletionMessageFunctionToolCallParam] = Field(default_factory=list)
    # 保持 ``ToolObservation`` 类型交接，不转成 dict 再按字符串键取回：字段改名时下游会在类型检查
    # 与测试里直接暴露（曾因按旧键读取，后台占位标记被静默丢弃）。``artifact_data`` 由 tools 节点
    # 剥掉，内部产物不进入 Run。
    observations: list[ToolObservation] = Field(default_factory=list)
    # 用户对 HITL 调用作出决定后，prepare_input 绕过模型组出单调用批次，携带 DecisionBody
    # 的 JSON 形态；本层不依赖 core 的消息类型）。tools 节点把它交给 handler，observe 节点据此把
    # 结果追加为消息、只更新占位的展示状态，而不是写 tool 消息。
    decision: dict[str, Any] | None = None

    def suspends(self) -> bool:
        """本批是否有调用要求挂起（见 ``ToolObservation.suspend``）。"""
        return any(observation.suspend for observation in self.observations)
