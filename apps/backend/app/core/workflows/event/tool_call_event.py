"""工具调用生命周期事件。

本模块只承载「一次工具调用从被模型请求到执行结束」这一单一职责，以及 run 终态时对
未决工具的批量收束。

不负责：工具的实际执行与权限审批、工具观察写回模型上下文（``RuntimeContextManager``）、
工具结果的展示摘要压缩。

待收口项：``ToolCallEventStatus`` 与 ``ConversationStateToolCallPart.status`` 目前是两处
同义字面量。event 位于 ``core`` 层，不能反向依赖 ``assistant_transport.state`` 的传输契约，
故在此内联声明；后续改造应把该词表下沉到 ``app.models.enums``，让事件契约与快照 part
引用同一事实源。
"""

from typing import Literal

from pydantic import Field

from app.core.workflows.event.conversation_event_envelope import ConversationEventEnvelope

# 工具调用状态词表，与快照 tool-call part 的 status 同义（详见模块 docstring 待收口项）。
ToolCallEventStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


class ToolCallCreatedEvent(ConversationEventEnvelope):
    """模型已请求一次工具调用。

    事实语义：模型产出了一个工具调用（参数已确定），Transport 侧应在 assistant 消息中
    建立对应的 tool-call part 并置 ``pending``。本事件在进入审批或执行**之前**发出，
    使前端能在工具真正跑起来之前就看到「模型打算做什么」。

    Attributes:
        tool_call_id: 工具调用在 Task 内唯一的稳定标识（模型提供或由执行链生成）。
        tool_name: 被调用工具名。
        args: 工具入参；非对象形态的入参在投影时归一为空对象。
        presentation: ``ToolDisplayHints`` 序列化后的静态展示声明；不进入模型上下文。

    异常:
        pydantic.ValidationError: 标识或工具名为空、``args`` 非对象，或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    type: Literal["tool_call_created"] = "tool_call_created"
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    args: dict[str, object] = Field(default_factory=dict)
    presentation: dict[str, object] = Field(default_factory=dict)


class ToolCallStatusChangedEvent(ConversationEventEnvelope):
    """一次工具调用的状态已经迁移。

    事实语义：工具已被开始执行，或已执行结束（成功 / 失败 / 取消），Transport 侧应把
    对应 tool-call part 迁移到目标状态并写入结果或错误。所有非终态与终态迁移统一由本
    类型表达，不为每个目标状态单开事件类型。

    Attributes:
        tool_call_id: 目标工具调用标识。
        status: 迁移后的状态。
        result: 工具输出的结构化结果；失败与取消时为 ``None``。
        error: 面向展示的错误摘要；仅失败时非空。
        data: 面向 UI 的结构化展示结果；不进入模型上下文。

    异常:
        pydantic.ValidationError: 标识为空、``status`` 取值非法，或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    type: Literal["tool_call_status_changed"] = "tool_call_status_changed"
    tool_call_id: str = Field(min_length=1)
    status: ToolCallEventStatus
    result: object | None = None
    error: str | None = None
    data: dict[str, object] | None = None


class ToolCallsSettledEvent(ConversationEventEnvelope):
    """该 Run 遗留的全部未决工具调用已被批量收束。

    事实语义：run 已进入终态（失败 / 取消 / 进程重启），其中仍处于 ``pending`` / ``running``
    的 tool-call part 不可能再收到单条状态迁移，需要一次性收束为终态以免前端永久转圈。
    本事件是**补偿性事实**，正常路径仍由 ``ToolCallStatusChangedEvent`` 逐条驱动。

    之所以需要独立类型：批量收束没有单一的 ``tool_call_id``，且语义是「清扫」而非
    「某一次调用的迁移」，与单条迁移的投影规则不同（不动 result）。

    Attributes:
        status: 收束后的目标状态，仅可为 ``failed`` 或 ``cancelled``。
        reason: 收束原因（如 ``"runtime_failed"`` / ``"executor_cancelled"`` /
            ``"backend_restarted"``），写入各 part 的 error 字段供前端展示。

    异常:
        pydantic.ValidationError: ``status`` 不是收束终态、``reason`` 为空，或出现未声明
            字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    type: Literal["tool_calls_settled"] = "tool_calls_settled"
    status: Literal["failed", "cancelled"]
    reason: str = Field(min_length=1)
