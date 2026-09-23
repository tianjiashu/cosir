"""消息 part 的文本内容与阶段事件。

本模块只承载「一条消息内 text / reasoning part 的内容追加与阶段收口」这一单一职责，覆盖 user
与 assistant 两侧；三者在投影上完全同构（都是「往某个 part 追加一段文本」或「把某个 part
标记为完成」），因此放在同一模块。每个事件把自身的投影逻辑实现在 ``plan`` 中。

不负责：消息骨架的建立（见 ``run_event.RunInitializedEvent``）、工具调用 part（见
``tool_call_event``）、消息终态（见 ``run_event.RunStatusChangedEvent``）。
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from app.assistant_transport.event.conversation_event_envelope import (
    ConversationEventEnvelope,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import (
    ConversationStatePart,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.constant import Constant

AssistantTextPartKind = Literal["text", "reasoning"]


class AssistantTextDeltaEvent(ConversationEventEnvelope):
    """模型输出的一段增量文本已经产生。

    事实语义：模型流式产出了一个 chunk 中的文本或 reasoning 内容，Transport 侧应把它
    增量追加到该 run 的 assistant 消息对应 part。本事件取代原
    ``react.streaming.ModelOutputDelta``：text 与 reasoning 形状完全相同，合并为一个
    类型由 ``part`` 区分，避免为两种通道各维护一套事件与投影分支。

    Attributes:
        part: 目标 part 类型，``"text"`` 为正文、``"reasoning"`` 为思考内容。
        delta: 非空增量文本。空增量不构成事实，不得发出。

    异常:
        pydantic.ValidationError: ``part`` 取值非法、``delta`` 为空串，或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    type: Literal["assistant_text_delta"] = "assistant_text_delta"
    part: AssistantTextPartKind
    delta: str = Field(min_length=1)

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划 assistant text/reasoning part 的增量追加或首次创建。

        参数:
            state: 当前 Task snapshot。

        返回:
            单条 ``append-text`` mutation（追加到 running part）或单条 ``set`` mutation
            （首次创建该类型 part）。

        异常:
            KeyError: 该 run 的 assistant message 不存在。

        副作用:
            无。
        """

        run_index = self._find_run(state, self.run_id)
        if state["runs"][run_index]["status"] in Constant.Run.TERMINAL_STATUSES:
            return []
        located = self._find_assistant_message(state, self.run_id)
        assert located is not None
        _, message_index = located
        parts: list[ConversationStatePart] = state["runs"][run_index]["messages"][
            message_index
        ]["parts"]
        for part_index in range(len(parts) - 1, -1, -1):
            part = parts[part_index]
            if (
                isinstance(part, dict)
                and part.get("type") == self.part
                and part.get("status") == "running"
            ):
                return [
                    ConversationStateMutation(
                        "append-text",
                        ("runs", run_index, "messages", message_index, "parts", part_index, "text"),
                        self.delta,
                    )
                ]
        return [
            ConversationStateMutation(
                "set",
                ("runs", run_index, "messages", message_index, "parts", len(parts)),
                {"type": self.part, "text": self.delta, "status": "running"},
            )
        ]


class AssistantPartClosedEvent(ConversationEventEnvelope):
    """一个 assistant text / reasoning part 已停止追加。

    事实语义：该 part 的内容已完整（流末、或 run 进入终态被收口），Transport 侧应把它
    从 ``running`` 标记为 ``completed``。前端据此关闭流式光标与「正在输入」态。

    单独成事件而不是由 applier 隐式推断：只有生产者知道一段内容何时算写完
    （模型可能在同一 part 内多次停顿，也可能被取消打断），推断会产生「永远不 completed」
    的悬挂 part。

    Attributes:
        part: 要收口的 part 类型，取值同 ``AssistantTextDeltaEvent.part``。

    异常:
        pydantic.ValidationError: ``part`` 取值非法，或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    type: Literal["assistant_part_closed"] = "assistant_part_closed"
    part: AssistantTextPartKind

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划 assistant 当前 part 的收口。

        参数:
            state: 当前 Task snapshot。

        返回:
            单条 ``set`` mutation（把 running part 标记为 completed）；未找到 running part
            时返回空序列。

        异常:
            KeyError: 该 run 的 assistant message 不存在。

        副作用:
            无。
        """

        run_index = self._find_run(state, self.run_id)
        if state["runs"][run_index]["status"] in Constant.Run.TERMINAL_STATUSES:
            return []
        located = self._find_assistant_message(state, self.run_id)
        assert located is not None
        _, message_index = located
        parts = state["runs"][run_index]["messages"][message_index]["parts"]
        for part_index in range(len(parts) - 1, -1, -1):
            part = parts[part_index]
            if (
                isinstance(part, dict)
                and part.get("type") == self.part
                and part.get("status") == "running"
            ):
                return [
                    ConversationStateMutation(
                        "set",
                        (
                            "runs",
                            run_index,
                            "messages",
                            message_index,
                            "parts",
                            part_index,
                            "status",
                        ),
                        "completed",
                    )
                ]
        return []
