"""上下文窗口占用事件。

模型 token 用量不再通过独立事件流式投影，而是在 Run 终态的
``RunStatusChangedEvent.usage_stats`` 中一次性写入；本模块只承载上下文窗口占用事实。
"""

import math
from collections.abc import Sequence
from typing import Literal

from pydantic import Field, StrictFloat, StrictInt, field_validator

from app.assistant_transport.event.conversation_event_envelope import ConversationEventEnvelope
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot


class ContextUsageUpdatedEvent(ConversationEventEnvelope):
    """上下文窗口占用比例已经更新。

    事实语义：上下文占用的最新测算结果已产生，Transport 侧应写入快照 ``context_usage_ratio``
    供前端上下文圆环展示。

    Attributes:
        ratio: 上下文窗口占用比例，``0.0`` ~ 1.0 之间的小数表示（超出 1.0 表示已超额，
            允许以便前端如实渲染超额态）。
        used_tokens: 可选的已占用 token 绝对数；仅用于排查与 tooltip，缺省为 ``None``。

    异常:
        pydantic.ValidationError: ``ratio`` 为负、``used_tokens`` 为负，或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    type: Literal["context_usage_updated"] = "context_usage_updated"
    ratio: StrictFloat = Field(ge=0.0)
    used_tokens: StrictInt | None = Field(default=None, ge=0)
    context_window_tokens: StrictInt | None = Field(default=None, ge=0)
    reproject: bool = False

    @field_validator("ratio")
    @classmethod
    def _require_finite_ratio(cls, value: float) -> float:
        """拒绝 NaN/Infinity，避免不可渲染的比例进入 snapshot。"""

        if not math.isfinite(value):
            raise ValueError("ratio must be finite")
        return value

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划上下文窗口占用比例的完整替换。

        参数:
            _state: 未使用；上下文占用与现有 snapshot 内容无关。

        返回:
            ``context_usage_ratio``、绝对已用 token 和窗口上限的 ``set`` mutation。

        异常:
            无。

        副作用:
            无。
        """

        return [
            ConversationStateMutation("set", ("context_usage_ratio",), self.ratio),
            ConversationStateMutation("set", ("context_usage_used",), self.used_tokens),
            ConversationStateMutation(
                "set", ("context_window_total",), self.context_window_tokens
            ),
        ]
