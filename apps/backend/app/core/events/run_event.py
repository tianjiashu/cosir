"""Run 生命周期事件。

本模块只承载「一次 Run 自身的创建、状态迁移与终态后的结果更新」。状态词表直接复用
``RunStatus``（领域枚举单一事实源）。

不负责：消息内容与工具调用生命周期（分别见 ``message_event``、``tool_call_event``）。
"""

from datetime import datetime
from typing import Literal

from pydantic import Field, StrictInt, StrictStr

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope
from app.core.workflow.execution_state.run_usage_stats import RunUsageStats
from app.models import RunError
from app.models.enums.run_status import RunStatus
from app.models.run_options import RunOptions


class RunInitializedEvent(WorkflowEventEnvelope):
    """一个 Run 已被创建并绑定到 Session。

    事实语义：Run 已落库，消费方据此把它设为 Session 当前活动；用户输入及 assistant 消息由各自的
    事件追加。Run 只前进：撤回后再发送新建 Run，不替换已有 Run。

    Attributes:
        created_at: Run 记录的创建时间，由领域侧从落库的记录填入；投影为 ``activity.startedAt``，
            前端据此做运行中的本地计时。投影器不读墙钟，所以必填。
        context_window_total: 创建 Run 时冻结的窗口上限，随该 Run 而非 Session 投影。
        model_config_id / options: 创建 Run 时冻结的发起配置，供前端执行运行期配置锁定。

    异常:
        pydantic.ValidationError: 信封字段非法或出现未声明字段时抛出。
    """

    # 判别式字段显式给出默认值：生产者不必重复书写字面量，判别式路由行为不变。
    type: Literal["run_initialized"] = "run_initialized"
    created_at: datetime
    context_window_total: StrictInt | None = Field(default=None, ge=0)
    model_config_id: StrictStr | None = None
    options: RunOptions = Field(default_factory=RunOptions)


class RunStatusChangedEvent(WorkflowEventEnvelope):
    """一次 Run 的执行状态已经迁移。

    事实语义：**迁移已经在领域侧完成并落库成功后**才发出本事件（event 是事实而非意图）。
    所有迁移统一由本类型表达，不为每个目标状态单开事件类型；「哪些迁移合法」的判定集中在
    领域侧一处。

    Attributes:
        status: 迁移后的 run 状态，取值受 ``RunStatus`` 约束。
        end_reason: 终态原因（如 ``"invalid_model_output"`` / ``"user_cancelled"`` /
            ``"backend_restarted"``）；非终态迁移为 ``None``。
        usage_stats: Run 终态时随事件附带的完整累计 token 用量；未获得 provider usage 时为
            ``None``。
        error: Run 终态的错误契约（稳定 ``code`` + UI 展示 ``message``）；模型 HTTP 失败时
            ``message`` 可直接来自响应体；非终态迁移与 ``completed`` 为 ``None``。
        changed_at: 迁移落库的时间，由领域侧从更新后的记录（``updated_at``）填入；终态时与
            ``activity.startedAt`` 相减得到结局的 ``durationMs``。时间必填，投影器不读墙钟。

    异常:
        pydantic.ValidationError: ``status`` 不在枚举内，或出现未声明字段时抛出。
    """

    type: Literal["run_status_changed"] = "run_status_changed"
    status: RunStatus
    end_reason: str | None = None
    usage_stats: RunUsageStats | None = None
    error: RunError | None = None
    changed_at: datetime
    # 已提交的最终模型响应身份，仅用于事件投影，不另存 Run 字段。
    response_sequence: int | None = None


class RunResultUpdatedEvent(WorkflowEventEnvelope):
    """已完成 Run 的结构化支线结果已落库；仅更新原因和用量，不重发状态事件。"""

    type: Literal["run_result_updated"] = "run_result_updated"
    end_reason: str | None = None
    usage_stats: RunUsageStats | None = None
