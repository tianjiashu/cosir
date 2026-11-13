"""运行中工具调用的运行期更新事件。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope


class TerminalCommandStartedData(BaseModel):
    """命令已经拉起：卡片的初始展示数据（命令与工作目录，还没有输出）。

    由工具在启动命令前经实时通道发出，先于任何输出块，所以运行中的卡片从启动起就能展开、看到命令；
    输出块只往这份展示数据上追加。展示形状归工具（与后台占位同一个构造函数），这里只是透传的映射。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["terminal_command_started"]
    display_data: dict[str, object]


class TerminalOutputDeltaData(BaseModel):
    """运行中命令的一段已解码输出。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["terminal_output_delta"]
    text: str = Field(min_length=1, max_length=4096)


ToolCallRuntimeUpdateData = Annotated[
    TerminalCommandStartedData | TerminalOutputDeltaData, Field(discriminator="kind")
]


class ToolCallRuntimeUpdateEvent(WorkflowEventEnvelope):
    """运行中工具调用产生了仅供 UI 展示的运行期数据。

    这类更新不是领域事实，也不持久化工具输出。``seq`` 在同一工具调用、同一更新种类内单调递增，
    使消费方能独立丢弃陈旧更新。
    """

    type: Literal["tool_call_runtime_update"] = "tool_call_runtime_update"
    tool_call_id: str = Field(min_length=1)
    seq: int = Field(ge=0)
    data: ToolCallRuntimeUpdateData
