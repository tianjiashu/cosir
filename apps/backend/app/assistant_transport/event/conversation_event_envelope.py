"""Conversation event 的共有信封字段、投影契约与共享快照辅助。

本模块只承载「所有 conversation event 都必须携带的定位与排查字段」「把事实投影为 snapshot
mutation 的抽象契约」以及「各事件 ``plan`` 共用的 snapshot 定位与消息骨架辅助函数」这一组
紧密相关的单一职责，不含任何具体事件类型；具体事件在各域模块中继承本信封并追加自己的
payload 字段与 ``plan`` 实现。

信封存在的意义：event 描述的是一条**已经发生的事实**（而不是待执行的意图），消费者必须知道
它作用于哪个 task / run、由哪一步产生、何时产生，才能在状态投影、结构化日志和事后排查中
唯一定位。其中 ``step_id`` 与 ``occurred_at`` 只服务于日志与排查，**不参与状态投影**。

投影契约：``plan`` 是基类声明的抽象方法，每个具体事件必须实现它——把自身事实翻译成一组
``ConversationStateMutation``。事件因此「自带投影逻辑」，projector 只需调用
``event.plan(state)`` 即可，无需按类型分派。``plan`` 只允许抛出 ``ValueError`` / ``KeyError``
等语义异常（非法状态迁移、引用的消息 / part / 工具不存在），由 projector 向上传播。

共享辅助：``_message`` / ``_find_assistant_message`` / ``_find_message_part`` / ``_find_tool``
是 ``ConversationEventEnvelope`` 的静态方法，供各事件 ``plan`` 复用：在
``ConversationStateSnapshot`` 中按业务键定位 message / part / 工具，以及构造 user /
assistant 消息骨架。它们无状态、不接触 storage / 网络，且只读取 ``state`` 不修改它；
内部重复的「按 run/role 定位 message」与「按 type 定位 part」逻辑已抽取为
``_locate_message`` / ``_locate_part`` 两个私有静态方法。

不负责：事件的分发、排序、持久化、合法性仲裁（分别由事件通道与 projector 承担），以及
mutation 之外的 snapshot 写语义（由各事件 ``plan`` 决定）。
"""

from abc import abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import ConversationStatePart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot


class ConversationEventEnvelope(BaseModel):
    """所有 conversation event 的共有信封与投影契约。

    提供「作用于谁、由哪一步产生、何时产生」的唯一定位信息，并声明 ``plan`` 抽象方法要求
    子类把事实投影为 snapshot mutation。本类是抽象类（``plan`` 为抽象方法），不可直接实例化，
    必须由具体事件子类实现 ``plan``。本类不承载任何业务 payload，也不描述如何把事实投影成
    Transport snapshot——具体投影由各子类在 ``plan`` 中实现。

    契约说明：

    - ``extra="forbid"``：event 是进程内契约，字段拼错必须立刻暴露而不是被静默丢弃。
    - ``frozen=True``：事实一旦产生即不可变，避免消费者改写后再被后续消费者读到脏值。

    Attributes:
        task_id: 事实所属 Task 标识，同时是 Transport snapshot 的聚合维度。
        run_id: 事实所属 Conversation Run 标识。
        event_id: 事件唯一标识，projector 用于去重。
        step_id: 产生该事实的 graph 步标识；纯排查用，不进 snapshot。
        occurred_at: 事实产生时刻（UTC，带时区）；纯排查用，不进 snapshot。

    异常:
        pydantic.ValidationError: ``task_id`` / ``run_id`` 非正整数，或出现未声明字段时抛出。
        NotImplementedError: 直接实例化抽象基类时抛出（``plan`` 未实现）。

    副作用:
        无；仅做字段校验，不触碰 storage、网络或运行时状态。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: int = Field(ge=1)
    # ContextUsageUpdatedEvent 无 run_id
    run_id: int | None = None
    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    step_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @abstractmethod
    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """把本事实投影为一组 snapshot mutation。

        参数:
            state: 当前 Task 的 Transport snapshot（只读，供定位消息 / part / 工具）。

        返回:
            需要施加到 snapshot 的 mutation 序列；无变化时返回空序列。

        异常:
            ValueError: 投影规则不满足（如非法工具状态迁移）。
            KeyError: 事件引用的消息 / part / 工具在 snapshot 中不存在。

        副作用:
            无；只产出 mutation 描述，不直接修改 ``state``。
        """
        raise NotImplementedError


    # ------------------------------------------------------------------ #
    # 共享 snapshot 导航辅助：供各事件 ``plan`` 复用；无状态、只读取不修改 state。
    # ------------------------------------------------------------------ #

    @staticmethod
    def _message(
        message_id: str,
        run_id: int | None,
        role: Literal["user", "assistant"],
        status: str,
        parts: list[ConversationStatePart],
    ) -> ConversationStateMessage:
        """构造 Transport user/assistant 消息骨架。

        assistant 消息不预置空 text part，避免模型先输出 reasoning 时把真实 part 顺序错误地
        固定为 ``text -> reasoning``。

        参数:
            message_id: 消息标识（``user-<run_id>`` / ``assistant-<run_id>``）。
            run_id: 所属 Conversation Run 标识。
            role: 消息角色（``user`` / ``assistant``）。
            status: 消息初始状态。
            parts: 消息初始 part 列表。

        返回:
            符合 ``ConversationStateMessage`` 契约的消息骨架字典。

        异常:
            无。

        副作用:
            无；返回新字典，不修改入参。
        """

        return {
            "id": message_id,
            "runId": run_id,
            "role": role,
            "status": status,
            "endReason": None,
            "parts": parts,
        }

    @staticmethod
    def _find_assistant_message(
        state: ConversationStateSnapshot,
        run_id: int | None,
        *,
        required: bool = True,
    ) -> int | None:
        """按 run 找到 assistant message 的下标。

        参数:
            state: 当前 Task snapshot。
            run_id: 目标 Run 标识。
            required: 未找到时是否抛出 ``KeyError``；``False`` 时返回 ``None``。

        返回:
            assistant message 在 ``state["messages"]`` 中的下标；``required=False`` 且未找到时
            返回 ``None``。

        异常:
            KeyError: ``required=True`` 且对应 assistant message 不存在。

        副作用:
            无。
        """

        index = ConversationEventEnvelope._locate_message(
            state, run_id=run_id, role="assistant"
        )
        if index is None and required:
            raise KeyError(f"assistant message for run {run_id} not found")
        return index

    @staticmethod
    def _find_message_part(
        state: ConversationStateSnapshot,
        run_id: int | None,
        role: str,
        part_type: str,
    ) -> tuple[int, int]:
        """按 run、role 与 part 类型定位消息 part。

        参数:
            state: 当前 Task snapshot。
            run_id: 目标 Run 标识。
            role: 目标消息角色。
            part_type: 目标 part 类型（``"text"`` / ``"reasoning"``）。

        返回:
            ``(message 下标, part 下标)`` 元组。

        异常:
            KeyError: 对应 part 不存在。

        副作用:
            无。
        """

        message_index = ConversationEventEnvelope._locate_message(
            state, run_id=run_id, role=role
        )
        if message_index is None:
            raise KeyError(f"{role} {part_type} part for run {run_id} not found")
        part_index = ConversationEventEnvelope._locate_part(
            state["messages"][message_index], part_type
        )
        if part_index is None:
            raise KeyError(f"{role} {part_type} part for run {run_id} not found")
        return message_index, part_index

    @staticmethod
    def _find_tool(
        state: ConversationStateSnapshot,
        tool_call_id: str,
    ) -> tuple[int, int]:
        """按 Task 内唯一 toolCallId 定位 tool part。

        参数:
            state: 当前 Task snapshot。
            tool_call_id: 工具调用标识。

        返回:
            ``(message 下标, part 下标)`` 元组。

        异常:
            KeyError: 对应 tool-call part 不存在。

        副作用:
            无。
        """

        for message_index, message in enumerate(state["messages"]):
            part_index = ConversationEventEnvelope._locate_part(
                message, "tool-call", tool_call_id=tool_call_id
            )
            if part_index is not None:
                return message_index, part_index
        raise KeyError(tool_call_id)

    @staticmethod
    def _locate_message(
        state: ConversationStateSnapshot,
        *,
        run_id: int | None,
        role: str | None = None,
    ) -> int | None:
        """在 snapshot 的 messages 中按 run 与可选 role 定位 message 下标。

        参数:
            state: 当前 Task snapshot。
            run_id: 目标 Run 标识；用于筛选 ``runId`` 匹配的 message。
            role: 可选的角色过滤（``user`` / ``assistant``）；省略时不按角色筛选。

        返回:
            首个同时满足 ``runId == run_id``（且 ``role`` 匹配，若给定）的 message 下标；
            未找到时返回 ``None``。

        异常:
            无。

        副作用:
            无。
        """

        for index, message in enumerate(state["messages"]):
            if message.get("runId") != run_id:
                continue
            if role is not None and message.get("role") != role:
                continue
            return index
        return None

    @staticmethod
    def _locate_part(
        message: ConversationStateMessage,
        part_type: str,
        *,
        tool_call_id: str | None = None,
    ) -> int | None:
        """在单条 message 的 parts 中按 type（与可选 toolCallId）定位 part 下标。

        参数:
            message: 待检索的消息字典。
            part_type: 目标 part 类型（如 ``"text"`` / ``"reasoning"`` / ``"tool-call"``）。
            tool_call_id: 可选的 tool-call 标识过滤；仅 ``part_type == "tool-call"`` 时需要。

        返回:
            首个 ``type`` 匹配（且 ``toolCallId`` 匹配，若给定）的 part 下标；
            未找到时返回 ``None``。

        异常:
            无。

        副作用:
            无。
        """

        for index, part in enumerate(message["parts"]):
            if not isinstance(part, dict) or part.get("type") != part_type:
                continue
            if tool_call_id is not None and part.get("toolCallId") != tool_call_id:
                continue
            return index
        return None
