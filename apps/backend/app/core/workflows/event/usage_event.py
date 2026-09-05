"""计量类事件（token 消耗与上下文占用）。

本模块只承载「一次 run 的资源计量」这一单一职责：模型 token 消耗与上下文窗口占用。
二者都是**展示与配额提示**性质的事实，不参与 Agent 控制流，因此与 run / message /
tool_call 事件分开成模块。

字段与 ``ConversationRunUsageStats.to_dict()`` 的键一一对应，使 applier 在投影时
无需推导或补齐逻辑——event 携带什么，快照就存什么。
"""

from typing import Literal

from pydantic import Field

from app.core.workflows.event.conversation_event_envelope import ConversationEventEnvelope


class ContextUsageUpdatedEvent(ConversationEventEnvelope):
    """上下文窗口占用比例已经更新。

    事实语义：上下文占用的最新测算结果已产生，Transport 侧应写入快照
    ``context_usage`` 供前端上下文圆环展示。

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


class UsageUpdatedEvent(ConversationEventEnvelope):
    """一次模型调用后的累计 token 用量已经更新。"""

    type: Literal["usage_updated"] = "usage_updated"
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cache_hit_tokens: int = Field(default=0, ge=0)
    cache_miss_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
