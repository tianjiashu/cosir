"""Conversation Run 生命周期事件。

本模块只承载「一次 Conversation Run 自身的创建与状态迁移」这一单一职责：run 骨架的
建立、run 执行状态的迁移（含终态）。run 内消息与工具的细节事实不在此模块。

状态词表直接复用 ``ConversationRunStatus``（领域枚举单一事实源），不在本模块重复字面量，
避免状态机在 event 层与落库层漂移。

不负责：用户输入的文本内容（见 ``message_event``）、工具调用生命周期
（见 ``tool_call_event``）、token 计量（见 ``usage_event``）。
"""

from typing import Literal

from pydantic import Field

from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflows.event.conversation_event_envelope import ConversationEventEnvelope
from app.models.enums.conversation_run_status import ConversationRunStatus


class RunInitializedEvent(ConversationEventEnvelope):
    """一个 Conversation Run 已被创建并绑定到 Task。

    事实语义：命令已被幂等占用、run 已落库，Transport 侧应为其建立 user / assistant
    两条空消息骨架与 ``run.runId`` 基线。本事件**不携带用户输入文本**——文本由紧随其后的
    ``UserInputAppendedEvent`` 追加，与流式 assistant 文本共用同一条 ``append-text`` 通道，
    使「run 已存在但输入尚未写完」这个中间态也是合法且可渲染的。

    Attributes:
        仅继承信封字段；本事件无额外 payload。

    异常:
        pydantic.ValidationError: 信封字段非法或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    # 判别式字段显式给出默认值：生产者不必重复书写字面量，判别式路由行为不变。
    type: Literal["run_initialized"] = "run_initialized"


class RunStatusChangedEvent(ConversationEventEnvelope):
    """一次 Conversation Run 的执行状态已经迁移。

    事实语义：**迁移已经在领域侧完成并落库成功后**才发出本事件（event 是事实而非意图）。
    发出方不负责再迁一次，applier 只把它投影为快照的 ``run.status`` 与对应 assistant
    消息的 ``status`` / ``endReason``。

    所有迁移（pending → running → completed / failed / cancelled，以及启动恢复、
    API 取消、执行器收口）统一由本类型表达，不为每个目标状态单开事件类型：
    类型数与 applier 分支数减半，且「哪些迁移合法」的判定集中在领域侧一处。

    Attributes:
        status: 迁移后的 run 状态，取值受 ``ConversationRunStatus`` 约束。
        end_reason: 终态原因（如 ``"invalid_model_output"`` / ``"user_cancelled"`` /
            ``"backend_restarted"``）；非终态迁移为 ``None``。

    异常:
        pydantic.ValidationError: ``status`` 不在枚举内，或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    type: Literal["run_status_changed"] = "run_status_changed"
    status: ConversationRunStatus
    end_reason: str | None = None
    usage_stats: ConversationRunUsageStats | None = None


class UserInputAppendedEvent(ConversationEventEnvelope):
    """用户输入文本已追加到该 Run 的 user 消息。

    事实语义：用户输入已落库为 canonical user 消息，Transport 侧应把这段文本增量追加到
    该 run 的 user 消息 text part。之所以叫「追加」而不是「设置」：与
    ``AssistantTextDeltaEvent`` 同构，复用同一条 ``append-text`` 投影通道，避免为
    「一次性写入」和「流式写入」维护两套投影逻辑。

    Attributes:
        text: 非空用户输入文本增量。

    异常:
        pydantic.ValidationError: ``text`` 为空串，或出现未声明字段时抛出。

    副作用:
        无；本事件只描述已发生的事实，不执行任何写入。
    """

    type: Literal["user_input_appended"] = "user_input_appended"
    text: str = Field(min_length=1)
