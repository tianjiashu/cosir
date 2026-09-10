"""计量类事件（token 消耗与上下文占用）。

本模块只承载「一次 run 的资源计量」这一单一职责：模型 token 消耗与上下文窗口占用。二者都是
**展示与配额提示**性质的事实，不参与 Agent 控制流，因此与 run / message / tool_call 事件分开
成模块。每个事件把自身的投影逻辑实现在 ``plan`` 中。

字段与 ``ConversationRunUsageStats.to_dict()`` 的键一一对应，使投影时无需推导或补齐逻辑——
event 携带什么，快照就存什么。
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

    事实语义：上下文占用的最新测算结果已产生，Transport 侧应写入快照 ``context_usage``
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
    context_revision: StrictInt | None = Field(default=None, ge=0)
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
            ``context_usage``、绝对已用 token 和窗口上限的 ``set`` mutation。

        异常:
            无。

        副作用:
            无。
        """

        if self.run_id is not None and state["run"].get("runId") != self.run_id:
            return []
        if (
            not self.reproject
            and self.context_revision is not None
            and state["context_revision"] is not None
            and self.context_revision <= state["context_revision"]
        ):
            return []
        if (
            not self.reproject
            and self.context_revision is None
            and state["context_revision"] is not None
        ):
            # 没有 revision 的旧/迟到事件无法证明顺序，不能覆盖已经确认的测量。
            return []
        context_revision = (
            self.context_revision
            if self.context_revision is not None
            else state["context_revision"]
        )
        return [
            ConversationStateMutation("set", ("context_usage",), self.ratio),
            ConversationStateMutation("set", ("context_revision",), context_revision),
            ConversationStateMutation("set", ("context_usage_used",), self.used_tokens),
            ConversationStateMutation(
                "set", ("context_window_total",), self.context_window_tokens
            ),
        ]


class UsageUpdatedEvent(ConversationEventEnvelope):
    """一次模型调用后的累计 token 用量已经更新。"""

    type: Literal["usage_updated"] = "usage_updated"
    input_tokens: StrictInt = Field(default=0, ge=0)
    output_tokens: StrictInt = Field(default=0, ge=0)
    total_tokens: StrictInt = Field(default=0, ge=0)
    cache_hit_tokens: StrictInt = Field(default=0, ge=0)
    cache_miss_tokens: StrictInt | None = Field(default=None, ge=0)
    reasoning_tokens: StrictInt = Field(default=0, ge=0)

    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划累计模型用量的完整替换。

        参数:
            _state: 未使用；累计用量由事件自带字段完整描述。

        返回:
            单条 ``set`` mutation，把 ``usage`` 设为事件携带的用量字典。

        异常:
            无。

        副作用:
            无。
        """

        if self.run_id is None or state["run"].get("runId") != self.run_id:
            return []
        current_usage = state["usage"]
        incoming_usage = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }
        if any(
            incoming_usage[key] is not None
            and current_usage[key] is not None
            and current_usage[key] > incoming_usage[key]
            for key in incoming_usage
        ):
            return []
        return [
            ConversationStateMutation("set", ("usage_run_id",), self.run_id),
            ConversationStateMutation(
                "set",
                ("usage",),
                {
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "total_tokens": self.total_tokens,
                    "cache_hit_tokens": self.cache_hit_tokens,
                    "cache_miss_tokens": self.cache_miss_tokens,
                    "reasoning_tokens": self.reasoning_tokens,
                },
            )
        ]
