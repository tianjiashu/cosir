"""工具调用生命周期事件。

本模块只承载「一次工具调用从被模型请求到执行结束」，以及 run 终态时对未决工具的批量收束。
工具调用状态词表的单一事实来源是 ``app.models.enums.tool_call_status.ToolCallEventStatus``。
"""

from typing import Literal

from pydantic import Field

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope
from app.models.enums.tool_call_status import ToolCallEventStatus
from app.models.message_metadata import ToolMessageMetadata


class ToolCallCreatedEvent(WorkflowEventEnvelope):
    """模型已请求一次工具调用。

    事实语义：模型产出了一个工具调用身份。参数可能仍在模型流中累积，由流收口时的
    ``ToolCallArgumentsEvent`` 写入。本事件在进入审批或执行**之前**发出，使消费方能在
    工具真正跑起来之前就看到「模型打算做什么」。

    Attributes:
        tool_call_id: 工具调用在 Session 内唯一的稳定标识（模型提供或由执行链生成）。
        tool_name: 被调用工具名。前端按它查工具目录表得到图标、动词与标题规则，后端不下发展示外壳。
        turn: 发起调用的响应所在的回合；投影按它找到（或建出）这一回合的回复。

    异常:
        pydantic.ValidationError: 标识或工具名为空，或出现未声明字段时抛出。
    """

    type: Literal["tool_call_created"] = "tool_call_created"
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    turn: int


class ToolCallArgumentsEvent(WorkflowEventEnvelope):
    """模型流收口后的调用参数（展示用解码）；不代表准入或执行，不改变调用状态。

    只来自模型输出，与冷重建同源：被拒绝的调用不会有 running，Hook 改写的业务参数也不是调用事实。
    """

    type: Literal["tool_call_arguments"] = "tool_call_arguments"
    tool_call_id: str = Field(min_length=1)
    args: dict[str, object]


class ToolCallStatusChangedEvent(WorkflowEventEnvelope):
    """一次工具调用的状态已经迁移。

    事实语义：工具已准备执行、开始执行，或已执行结束（成功 / 失败 / 取消）。所有非终态与终态
    迁移统一由本类型表达。

    时序：``running`` 只在调用通过准入、即将交给 handler 时发出；终态由两处发出——**执行即投影**
    （``tools_node`` 在每个调用结束时发出，早于 ``ToolMessage`` 落 canonical context）与
    **结算兜底**（``settle_observations``，在写 ``ToolMessage`` 之后）。取消不得早于执行层的强杀与
    输出排空：执行层在检出取消的瞬间只产出观察与日志，不发状态事件（见 ``tool_terminal_projection`` 的时序说明）。两处对同一调用
    产生相同终态，由消费方按自迁移幂等吸收。

    Attributes:
        tool_call_id: 目标工具调用标识。
        result: 这次迁移后的结果行事实，与持久化的 metadata 同一个形状（``ToolMessageMetadata``）：
            状态、短错误提示、展示数据、后台记录与交付记录作为一个整体传递，事件、落库与快照投影之间
            不再逐个抄字段。只迁移状态的事件（开始执行、修补中断）只带 ``status``。投影按
            「有才写」吸收：展示数据之外，后台记录与交付记录为空时 part 上已有的保留（见
            ``project_tool_result``）。

    异常:
        pydantic.ValidationError: 标识为空、``status`` 取值非法，或出现未声明字段时抛出。
    """

    type: Literal["tool_call_status_changed"] = "tool_call_status_changed"
    tool_call_id: str = Field(min_length=1)
    result: ToolMessageMetadata

    @property
    def status(self) -> ToolCallEventStatus:
        return self.result.status
