"""Task 当前活动（最新 Run 摘要）的 Transport 契约。"""

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_error import ConversationStateError
from app.assistant_transport.state.conversation_state_usage import ConversationStateUsage
from app.models.conversation_run_options import ConversationRunOptions


class ConversationRunLaunch(TypedDict):
    """Run 冻结的发起配置；前端据此锁定运行期配置控件（运行期配置锁定）。

    ``modelConfigId`` 在模型配置被删除后为 ``None``。
    """

    modelConfigId: int | None
    reasoningEffort: str | None
    banTools: list[str]


def run_launch_snapshot(
    model_config_id: int | None, options: ConversationRunOptions
) -> ConversationRunLaunch:
    return ConversationRunLaunch(
        modelConfigId=model_config_id,
        reasoningEffort=options.reasoning_effort,
        banTools=list(options.ban_tools),
    )


class ConversationStateActivity(TypedDict):
    """Task 当前活动：最新 Run 的摘要，对话界面判断运行中、停止、恢复、配置锁定与用量都只读它。

    是最新 Run 的投影而不是第二套状态机：事实源仍是 ``ConversationRunModel.status``。
    ``runId`` 只供事件路由（旧 Run 的迟到事件不能覆盖它）与诊断，前端不按它比对。
    ``error`` 只在 Run 进入失败 / 取消终态时携带 ``code`` + UI ``message``。
    """

    runId: int
    status: str
    endReason: str | None
    error: ConversationStateError | None
    usage: ConversationStateUsage | None
    contextWindowTotal: int | None
    launch: ConversationRunLaunch
