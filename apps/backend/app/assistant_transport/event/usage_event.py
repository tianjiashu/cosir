"""计量类事件（token 消耗与上下文占用）。

本模块只承载「一次 run 的资源计量」这一单一职责：模型 token 消耗与上下文窗口占用。二者都是
**展示与配额提示**性质的事实，不参与 Agent 控制流，因此与 run / message / tool_call 事件分开
成模块。每个事件把自身的投影逻辑实现在 ``plan`` 中。

字段与 ``ConversationRunUsageStats.to_dict()`` 的键一一对应，使投影时无需推导或补齐逻辑——
event 携带什么，快照就存什么。
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import Field

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
    ratio: float = Field(ge=0.0)
    used_tokens: int | None = Field(default=None, ge=0)

    def plan(
        self,
        _state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """规划上下文窗口占用比例的完整替换。

        参数:
            _state: 未使用；上下文占用与现有 snapshot 内容无关。

        返回:
            单条 ``set`` mutation，把 ``context_usage`` 设为本次 ``ratio``。

        异常:
            无。

        副作用:
            无。
        """

        return [ConversationStateMutation("set", ("context_usage",), self.ratio)]


class UsageUpdatedEvent(ConversationEventEnvelope):
    """一次模型调用后的累计 token 用量已经更新。"""

    type: Literal["usage_updated"] = "usage_updated"
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cache_hit_tokens: int = Field(default=0, ge=0)
    cache_miss_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)

    def plan(
        self,
        _state: ConversationStateSnapshot,
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

        return [
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
