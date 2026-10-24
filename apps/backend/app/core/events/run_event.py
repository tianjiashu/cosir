"""Run 生命周期事件。

本模块只承载「一次 Run 自身的创建、状态迁移与终态后的结果更新」。状态词表直接复用
``ConversationRunStatus``（领域枚举单一事实源）。

不负责：消息内容与工具调用生命周期（分别见 ``message_event``、``tool_call_event``）。
"""

from typing import Literal

from pydantic import Field, StrictInt

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.models import ConversationRunError
from app.models.conversation_run_options import ConversationRunOptions
from app.models.enums.conversation_run_status import ConversationRunStatus


class RunInitializedEvent(WorkflowEventEnvelope):
    """一个 Run 已被创建并绑定到 Task。

    事实语义：Run 已落库，消费方据此建立 Run 基线；用户输入及 assistant 消息由各自的事件追加。

    Attributes:
        replace_existing: 编辑重跑时是否替换已有 Run 的骨架。
        context_window_total: 创建 Run 时冻结的窗口上限，随该 Run 而非 Task 投影。
        model_config_id / options: 创建 Run 时冻结的发起配置，供前端执行运行期配置锁定。

    异常:
        pydantic.ValidationError: 信封字段非法或出现未声明字段时抛出。
    """

    # 判别式字段显式给出默认值：生产者不必重复书写字面量，判别式路由行为不变。
    type: Literal["run_initialized"] = "run_initialized"
    replace_existing: bool = False
    context_window_total: StrictInt | None = Field(default=None, ge=0)
    model_config_id: StrictInt | None = None
    options: ConversationRunOptions = Field(default_factory=ConversationRunOptions)


class RunStatusChangedEvent(WorkflowEventEnvelope):
    """一次 Run 的执行状态已经迁移。

    事实语义：**迁移已经在领域侧完成并落库成功后**才发出本事件（event 是事实而非意图）。
    所有迁移统一由本类型表达，不为每个目标状态单开事件类型；「哪些迁移合法」的判定集中在
    领域侧一处。

    Attributes:
        status: 迁移后的 run 状态，取值受 ``ConversationRunStatus`` 约束。
        end_reason: 终态原因（如 ``"invalid_model_output"`` / ``"user_cancelled"`` /
            ``"backend_restarted"``）；非终态迁移为 ``None``。
        usage_stats: Run 终态时随事件附带的完整累计 token 用量；未获得 provider usage 时为
            ``None``。
        error: Run 终态的错误契约（稳定 ``code`` + UI 展示 ``message``）；模型 HTTP 失败时
            ``message`` 可直接来自响应体；非终态迁移与 ``completed`` 为 ``None``。

    异常:
        pydantic.ValidationError: ``status`` 不在枚举内，或出现未声明字段时抛出。
    """

    type: Literal["run_status_changed"] = "run_status_changed"
    status: ConversationRunStatus
    end_reason: str | None = None
    usage_stats: ConversationRunUsageStats | None = None
    error: ConversationRunError | None = None


class RunResultUpdatedEvent(WorkflowEventEnvelope):
    """已完成 Run 的结构化支线结果已落库；仅更新原因和用量，不重发状态事件。"""

    type: Literal["run_result_updated"] = "run_result_updated"
    end_reason: str | None = None
    usage_stats: ConversationRunUsageStats | None = None
