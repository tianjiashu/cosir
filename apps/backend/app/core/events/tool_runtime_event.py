"""运行中工具调用的运行期更新事件。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope


class TerminalOutputDeltaData(BaseModel):
    """One decoded terminal-output chunk for the running tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["terminal_output_delta"]
    text: str = Field(min_length=1, max_length=4096)


ToolCallRuntimeUpdateData = Annotated[TerminalOutputDeltaData, Field(discriminator="kind")]


class ToolCallRuntimeUpdateEvent(WorkflowEventEnvelope):
    """运行中工具调用产生了仅供 UI 展示的运行期数据。

    这类更新不是领域事实，也不持久化工具输出。``seq`` 在同一工具调用、同一更新种类内单调递增，
    使消费方能独立丢弃陈旧更新。
    """

    type: Literal["tool_call_runtime_update"] = "tool_call_runtime_update"
    tool_call_id: str = Field(min_length=1)
    seq: int = Field(ge=0)
    data: ToolCallRuntimeUpdateData
