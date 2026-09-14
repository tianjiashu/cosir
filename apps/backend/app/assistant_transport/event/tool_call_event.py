"""工具调用生命周期事件。

本模块只承载「一次工具调用从被模型请求到执行结束」这一单一职责，以及 run 终态时对未决工具的
批量收束。每个事件把自身的投影逻辑实现在 ``plan`` 中。

工具调用状态词表的「单一事实来源」已下沉至 ``app.models.enums.tool_call_status``
（``ToolCallEventStatus``）：本模块的 ``ToolCallStatusChangedEvent.status`` 与 Transport
snapshot 的 ``ConversationStateToolCallPart.status`` 共用同一 Literal，避免两处字面量漂移。
"""

import copy
from collections.abc import Sequence
from typing import Literal, cast

from pydantic import Field

from app.assistant_transport.event.conversation_event_envelope import (
    ConversationEventEnvelope,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import ConversationStateToolCallPart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.logging.logger import log
from app.models.enums.tool_call_status import ToolCallEventStatus


class ToolCallCreatedEvent(ConversationEventEnvelope):
    """模型已请求一次工具调用。

    事实语义：模型产出了一个工具调用身份，Transport 侧应在 assistant 消息中建立对应的
    tool-call part 并置 ``pending``。参数可能仍在模型流中累积，由后续
    ``ToolCallStatusChangedEvent.args`` 写入。本事件在进入审批或执行**之前**发出，
    使前端能在工具真正跑起来之前就看到「模型打算做什么」。

    Attributes:
        tool_call_id: 工具调用在 Task 内唯一的稳定标识（模型提供或由执行链生成）。
        tool_name: 被调用工具名。
        presentation: 「怎么展示」的静态外壳声明，由 ``ToolDefinition.display``
            （``ToolDisplayHints``）经 ``to_dict()`` 序列化得到，**同一种工具每次调用
            完全相同**。只含字面量字段（``verb`` 动词、``icon`` 图标、``surface`` 展示面、
            ``expandable`` 是否可展开、``expand_layout`` 展开布局、``default_open`` 默认展开、
            ``show_result`` 是否展示模型结果）；
            不含 Callable、摘要文本或条目内容。前端据此决定外壳布局与图标。
            **不进入模型上下文**。
        本事件只携带工具身份与静态展示声明，不携带参数或执行结果。

    异常:
        pydantic.ValidationError: 标识或工具名为空，或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    type: Literal["tool_call_created"] = "tool_call_created"
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    presentation: dict[str, object] = Field(default_factory=dict)

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划 tool-call part 的建立。

        参数:
            state: 当前 Task snapshot。

        返回:
            单条 ``set`` mutation（在 assistant 消息尾部新建 tool-call part）；若同名
            toolCallId 已存在则回空列表（幂等）；若该 Run 已是终态或 assistant 消息骨架
            缺失，则回空列表并记 warning。

        异常:
            无（终态 Run 与缺失骨架都降级为跳过，不以异常中断 Run）。

        副作用:
            无；跳过时额外写一条 warning 日志。
        """

        run_index = self._find_run(state, self.run_id)
        if state["runs"][run_index]["status"] in {"completed", "failed", "cancelled"}:
            return []
        located = self._find_assistant_message(state, self.run_id, required=False)
        if located is None:
            # assistant 骨架缺失属于展示事实不完整，不得以断言中断整个 Run。
            log.warning(
                "tool_call_part_creation_skipped_missing_assistant_message",
                extra={
                    "msg": "assistant 消息骨架缺失，跳过 tool-call part 建立",
                    "data": {"run_id": self.run_id, "tool_call_id": self.tool_call_id},
                },
            )
            return []
        _, message_index = located
        parts = state["runs"][run_index]["messages"][message_index]["parts"]
        if any(
            isinstance(part, dict) and part.get("toolCallId") == self.tool_call_id for part in parts
        ):
            return []
        return [
            ConversationStateMutation(
                "set",
                ("runs", run_index, "messages", message_index, "parts", len(parts)),
                {
                    "type": "tool-call",
                    "toolCallId": self.tool_call_id,
                    "toolName": self.tool_name,
                    "status": "pending",
                    "error": None,
                    "presentation": copy.deepcopy(self.presentation),
                    "isError": False,
                    "approvalRequestId": None,
                },
            )
        ]


class ToolCallStatusChangedEvent(ConversationEventEnvelope):
    """一次工具调用的状态已经迁移。

    事实语义：工具已准备执行、开始执行，或已执行结束（成功 / 失败 / 取消），Transport
    侧应把对应 tool-call part 迁移到目标状态，并按需写入已累积的参数、错误与展示数据
    （均不进入模型上下文）。
    所有非终态与终态迁移统一由本类型表达，不为每个目标状态单开事件类型。

    Attributes:
        tool_call_id: 目标工具调用标识。
        status: 迁移后的状态。
        args: 已完成解析的工具入参；通常随 ``running`` 一起发送，为 ``None`` 时保留已有参数。
        error: 面向展示的短错误提示；仅失败时非空，不能承载完整诊断。
        display_data: 面向 UI 的结构化展示结果；不进入模型上下文。

    异常:
        pydantic.ValidationError: 标识为空、``status`` 取值非法，或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    type: Literal["tool_call_status_changed"] = "tool_call_status_changed"
    tool_call_id: str = Field(min_length=1)
    status: ToolCallEventStatus
    args: dict[str, object] | None = None
    error: str | None = None
    display_data: dict[str, object] | None = None

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划工具调用状态的一致迁移。

        参数:
            state: 当前 Task snapshot。

        返回:
            更新 ``status`` / ``args`` / ``error`` / ``isError``（及可选 ``display_data``）的
            mutation 列表；对应 tool-call part 不存在时返回空列表并记 warning。

        异常:
            ValueError: 目标状态不是当前状态允许迁移到的状态。

        副作用:
            无；part 缺失时额外写一条 warning 日志。
        """

        located = self._find_tool(state, self.tool_call_id, required=False)
        if located is None:
            # 状态事件不携带 toolName / presentation，缺少 part 时无法补建合法 part；此处只
            # 降级为「展示事实缺失」并继续后续投影，绝不让展示层缺口中断 Agent 执行。
            log.warning(
                "tool_call_part_missing_for_status_change",
                extra={
                    "msg": "工具调用 part 缺失，跳过本次状态投影",
                    "data": {
                        "run_id": self.run_id,
                        "tool_call_id": self.tool_call_id,
                        "status": self.status,
                    },
                },
            )
            return []
        run_index, message_index, part_index = located
        part = cast(
            ConversationStateToolCallPart,
            state["runs"][run_index]["messages"][message_index]["parts"][part_index],
        )
        current = str(part["status"])
        allowed = {
            "pending": {"pending", "running", "completed", "failed", "cancelled"},
            "running": {"running", "completed", "failed", "cancelled"},
            "completed": {"completed"},
            "failed": {"failed"},
            "cancelled": {"cancelled"},
        }
        if current in {"completed", "failed", "cancelled"} and self.status != current:
            return []
        if self.status not in allowed[current]:
            raise ValueError(f"invalid tool transition {current} -> {self.status}")
        base = ("runs", run_index, "messages", message_index, "parts", part_index)
        mutations: list[ConversationStateMutation] = [
            ConversationStateMutation("set", (*base, "status"), self.status),
            ConversationStateMutation(
                "set",
                (*base, "error"),
                self.error if self.status in {"failed", "cancelled"} else None,
            ),
            ConversationStateMutation("set", (*base, "isError"), self.status == "failed"),
        ]
        if self.args is not None:
            mutations.insert(
                1,
                ConversationStateMutation("set", (*base, "args"), copy.deepcopy(self.args)),
            )
        if self.display_data is not None:
            mutations.insert(
                2,
                ConversationStateMutation(
                    "set", (*base, "display_data"), copy.deepcopy(self.display_data)
                ),
            )
        return mutations


class ToolCallsSettledEvent(ConversationEventEnvelope):
    """该 Run 遗留的全部未决工具调用已被批量收束。

    事实语义：run 已进入终态（失败 / 取消 / 进程重启），其中仍处于 ``pending`` / ``running``
    的 tool-call part 不可能再收到单条状态迁移，需要一次性收束为终态以免前端永久转圈。
    本事件是**补偿性事实**，正常路径仍由 ``ToolCallStatusChangedEvent`` 逐条驱动。

    之所以需要独立类型：批量收束没有单一的 ``tool_call_id``，且语义是「清扫」而非
    「某一次调用的迁移」，与单条迁移的投影规则不同（只迁移状态、错误与 isError，不写展示数据）。

    Attributes:
        status: 收束后的目标状态，仅可为 ``failed`` 或 ``cancelled``。
        reason: 内部收束原因（如 ``"runtime_failed"`` / ``"executor_cancelled"`` /
            ``"backend_restarted"``），不直接写入前端；投影为固定短提示。

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
            的 mutation 列表（只迁移状态与错误）。

        异常:
            无。

        副作用:
            无。
        """

        mutations: list[ConversationStateMutation] = []
        run_index = self._find_run(state, self.run_id)
        for message_index, message in enumerate(state["runs"][run_index]["messages"]):
            for part_index, part in enumerate(message["parts"]):
                if (
                    not isinstance(part, dict)
                    or part.get("type") != "tool-call"
                    or part.get("status") not in {"pending", "running"}
                ):
                    continue
                base = ("runs", run_index, "messages", message_index, "parts", part_index)
                mutations.extend(
                    [
                        ConversationStateMutation("set", (*base, "status"), self.status),
                        ConversationStateMutation(
                            "set",
                            (*base, "error"),
                            "已取消" if self.status == "cancelled" else "执行异常",
                        ),
                        ConversationStateMutation(
                            "set", (*base, "isError"), self.status == "failed"
                        ),
                    ]
                )
        return mutations
