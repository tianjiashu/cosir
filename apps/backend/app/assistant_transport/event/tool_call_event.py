"""工具调用生命周期事件。

本模块只承载「一次工具调用从被模型请求到执行结束」这一单一职责，以及 run 终态时对未决工具的
批量收束。每个事件把自身的投影逻辑实现在 ``plan`` 中。

待收口项：``ToolCallEventStatus`` 与 ``ConversationStateToolCallPart.status`` 目前是两处同义
字面量。event 现位于 ``assistant_transport`` 层，可引用传输契约，但为避免与 snapshot part
状态词表漂移，此处仍内联声明，后续应把该词表下沉到 ``app.models.enums`` 让两者引用同一事实源。
"""

import copy
from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from app.assistant_transport.event.conversation_event_envelope import ConversationEventEnvelope
from app.assistant_transport.event.snapshot_locators import (
    _find_assistant_message,
    _find_tool,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot

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
        data: 执行前即可安全展示的结构化 UI 数据；不进入模型上下文。

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
    data: dict[str, object] | None = None

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划 tool-call part 的建立。

        参数:
            state: 当前 Task snapshot。

        返回:
            单条 ``set`` mutation（在 assistant 消息尾部新建 tool-call part）；若同名
            toolCallId 已存在则回空列表（幂等）。

        异常:
            KeyError: 该 run 的 assistant message 不存在。

        副作用:
            无。
        """

        message_index = _find_assistant_message(state, self.run_id)
        assert message_index is not None
        parts = state["messages"][message_index]["parts"]
        if any(
            isinstance(part, dict) and part.get("toolCallId") == self.tool_call_id
            for part in parts
        ):
            return []
        return [
            ConversationStateMutation(
                "set",
                ("messages", message_index, "parts", len(parts)),
                {
                    "type": "tool-call",
                    "toolCallId": self.tool_call_id,
                    "toolName": self.tool_name,
                    "status": "pending",
                    "args": copy.deepcopy(self.args),
                    "result": None,
                    "error": None,
                    "presentation": copy.deepcopy(self.presentation),
                    "data": copy.deepcopy(self.data) if self.data is not None else None,
                    "isError": False,
                    "approvalRequestId": None,
                },
            )
        ]


class ToolCallStatusChangedEvent(ConversationEventEnvelope):
    """一次工具调用的状态已经迁移。

    事实语义：工具已被开始执行，或已执行结束（成功 / 失败 / 取消），Transport 侧应把
    对应 tool-call part 迁移到目标状态并写入结果或错误。所有非终态与终态迁移统一由本
    类型表达，不为每个目标状态单开事件类型。

    Attributes:
        tool_call_id: 目标工具调用标识。
        status: 迁移后的状态。
        result: UI-safe 工具输出；失败与取消时为 ``None``，不得填入模型正文。
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

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划工具调用状态及结果的一致迁移。

        参数:
            state: 当前 Task snapshot。

        返回:
            更新 ``status`` / ``result`` / ``error`` / ``isError``（及可选 ``data``）的
            mutation 列表。

        异常:
            ValueError: 目标状态不是当前状态允许迁移到的状态。
            KeyError: 对应 tool-call part 不存在。

        副作用:
            无。
        """

        message_index, part_index = _find_tool(state, self.tool_call_id)
        part = state["messages"][message_index]["parts"][part_index]
        current = str(part["status"])
        allowed = {
            "pending": {"pending", "running", "completed", "failed", "cancelled"},
            "running": {"running", "completed", "failed", "cancelled"},
            "completed": {"completed"},
            "failed": {"failed"},
            "cancelled": {"cancelled"},
        }
        if self.status not in allowed[current]:
            raise ValueError(f"invalid tool transition {current} -> {self.status}")
        base = ("messages", message_index, "parts", part_index)
        mutations: list[ConversationStateMutation] = [
            ConversationStateMutation("set", (*base, "status"), self.status),
            ConversationStateMutation(
                "set", (*base, "result"), self.result if self.status == "completed" else None
            ),
            ConversationStateMutation(
                "set",
                (*base, "error"),
                self.error if self.status in {"failed", "cancelled"} else None,
            ),
            ConversationStateMutation("set", (*base, "isError"), self.status == "failed"),
        ]
        if self.data is not None:
            mutations.insert(
                2,
                ConversationStateMutation(
                    "set", (*base, "data"), copy.deepcopy(self.data)
                ),
            )
        return mutations


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

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划指定 Run 下所有未决工具调用的补偿性收束。

        参数:
            state: 当前 Task snapshot。

        返回:
            对该 run 下所有仍处于 ``pending`` / ``running`` 的 tool-call part 施加终态迁移
            的 mutation 列表（不动 ``result``）。

        异常:
            无。

        副作用:
            无。
        """

        mutations: list[ConversationStateMutation] = []
        for message_index, message in enumerate(state["messages"]):
            if message.get("runId") != self.run_id:
                continue
            for part_index, part in enumerate(message["parts"]):
                if (
                    not isinstance(part, dict)
                    or part.get("type") != "tool-call"
                    or part.get("status") not in {"pending", "running"}
                ):
                    continue
                base = ("messages", message_index, "parts", part_index)
                mutations.extend(
                    [
                        ConversationStateMutation("set", (*base, "status"), self.status),
                        ConversationStateMutation("set", (*base, "result"), None),
                        ConversationStateMutation("set", (*base, "error"), self.reason),
                        ConversationStateMutation(
                            "set", (*base, "isError"), self.status == "failed"
                        ),
                    ]
                )
        return mutations
