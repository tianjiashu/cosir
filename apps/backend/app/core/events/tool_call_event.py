"""工具调用生命周期事件。

本模块只承载「一次工具调用从被模型请求到执行结束」，以及 run 终态时对未决工具的批量收束。
工具调用状态词表的单一事实来源是 ``app.models.enums.tool_call_status.ToolCallEventStatus``。
"""

from typing import Literal

from pydantic import Field

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope
from app.models.enums.tool_call_status import ToolCallEventStatus


class ToolCallCreatedEvent(WorkflowEventEnvelope):
    """模型已请求一次工具调用。

    事实语义：模型产出了一个工具调用身份。参数可能仍在模型流中累积，由后续
    ``ToolCallStatusChangedEvent.args`` 写入。本事件在进入审批或执行**之前**发出，使消费方能在
    工具真正跑起来之前就看到「模型打算做什么」。

    Attributes:
        tool_call_id: 工具调用在 Task 内唯一的稳定标识（模型提供或由执行链生成）。
        tool_name: 被调用工具名。
        presentation: 「怎么展示」的静态外壳声明，由 ``ToolDefinition.display``
            （``ToolDisplayHints``）经 ``to_dict()`` 序列化得到，**同一种工具每次调用完全相同**；
            不含 Callable、摘要文本或条目内容，**不进入模型上下文**。

    异常:
        pydantic.ValidationError: 标识或工具名为空，或出现未声明字段时抛出。
    """

    type: Literal["tool_call_created"] = "tool_call_created"
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    presentation: dict[str, object] = Field(default_factory=dict)


class ToolCallStatusChangedEvent(WorkflowEventEnvelope):
    """一次工具调用的状态已经迁移。

    事实语义：工具已准备执行、开始执行，或已执行结束（成功 / 失败 / 取消）。所有非终态与终态
    迁移统一由本类型表达。

    时序：``running`` 只在调用通过准入、即将交给 handler 时发出；终态由两处发出——**执行即投影**
    （``run_tool_batch`` 在每个调用结束时发出，早于 ``ToolMessage`` 落 canonical context）与
    **结算兜底**（``settle_observations``，在写 ``ToolMessage`` 之后）。取消不得早于执行层的强杀与输出排空：执行层在检出取消的瞬间只产出
    观察与日志，不发状态事件（见 ``tool_terminal_projection`` 的时序说明）。两处对同一调用
    产生相同终态，由消费方按自迁移幂等吸收。

    Attributes:
        tool_call_id: 目标工具调用标识。
        status: 迁移后的状态。
        args: 已完成解析的工具入参；通常随 ``running`` 一起发送，为 ``None`` 时保留已有参数。
        error: 面向展示的短错误提示；仅失败时非空，不能承载完整诊断。
        display_data: 面向 UI 的结构化展示结果；不进入模型上下文。
        team_run_id: 可选的 TeamRun 定位信息；不承载运行状态或错误正文。

    异常:
        pydantic.ValidationError: 标识为空、``status`` 取值非法，或出现未声明字段时抛出。
    """

    type: Literal["tool_call_status_changed"] = "tool_call_status_changed"
    tool_call_id: str = Field(min_length=1)
    status: ToolCallEventStatus
    args: dict[str, object] | None = None
    error: str | None = None
    display_data: dict[str, object] | None = None
    team_run_id: int | None = Field(default=None, gt=0)


class ToolCallsSettledEvent(WorkflowEventEnvelope):
    """该 Run 遗留的全部未决工具调用已被批量收束。

    事实语义：run 已进入终态（失败 / 取消 / 进程重启），其中仍处于 ``pending`` / ``running``
    的工具调用不可能再收到单条状态迁移。本事件是**补偿性事实**，正常路径仍由
    ``ToolCallStatusChangedEvent`` 逐条驱动；批量收束没有单一的 ``tool_call_id``，因此独立成类型。

    Attributes:
        status: 收束后的目标状态，仅可为 ``failed`` 或 ``cancelled``。
        reason: 内部收束原因（如 ``"runtime_failed"`` / ``"executor_cancelled"`` /
            ``"backend_restarted"``），不直接展示给用户。

    异常:
        pydantic.ValidationError: ``status`` 不是收束终态、``reason`` 为空，或出现未声明
            字段时抛出。
    """

    type: Literal["tool_calls_settled"] = "tool_calls_settled"
    status: Literal["failed", "cancelled"]
    reason: str = Field(min_length=1)
