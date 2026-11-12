"""Task 当前活动（最新 Run 摘要）的 Transport 契约。"""

from typing_extensions import TypedDict

from app.core.events.model_activity_event import ModelActivityPhase
from app.models.conversation_run_options import ConversationRunOptions
from app.models.conversation_run_record import ConversationRunError
from app.models.conversation_run_usage import ConversationRunUsage


class ModelActivity(TypedDict):
    """当前模型步的瞬时输出阶段；不持久化、不参与调度。"""

    stepId: str
    phase: ModelActivityPhase


class ConversationRunLaunch(TypedDict):
    """Run 冻结的发起配置；前端据此锁定运行期配置控件（运行期配置锁定）。

    ``modelConfigId`` 是「连接 id/模型名」；模型从配置文件删除后仍保留原 id，
    前端按目录查不到即视为已删除。
    """

    modelConfigId: str | None
    reasoningEffort: str | None
    disabledToolGroups: list[str]


def run_launch_snapshot(
    model_config_id: str | None, options: ConversationRunOptions
) -> ConversationRunLaunch:
    return ConversationRunLaunch(
        modelConfigId=model_config_id,
        reasoningEffort=options.reasoning_effort,
        disabledToolGroups=list(options.disabled_tool_groups),
    )


class ConversationStateActivity(TypedDict):
    """Task 当前活动：最新 Run 的状态摘要，对话界面判断运行中、停止、恢复、配置锁定与用量都只读它。

    是最新 Run 的投影而不是第二套状态机：事实源仍是 ``ConversationRunModel.status``。Run 身份不上
    wire：``turn`` 是这个 Run 最后一个可见回合（还没有可见回合时为 ``None``），结局写到这一回合的
    回复上，前端据此认出「当前回复」；``resumable`` 是后端的恢复判定（cancelled / failed 且最后一个
    回合没被撤回），前端不另推断。投影保证：所有 Run 事件都在这个 Run 仍是当前活动时同步发出，所以
    事件不必再按 Run 号路由。
    ``error`` 只在 Run 进入失败 / 取消终态时携带 ``code`` + UI ``message``。``startedAt`` 是 Run
    记录的创建时间（ISO 8601），前端据此做运行中的本地计时；终态耗时在 ``outcome.durationMs``。
    """

    modelActivity: ModelActivity | None
    status: str
    turn: int | None
    resumable: bool
    startedAt: str
    endReason: str | None
    error: ConversationRunError | None
    usage: ConversationRunUsage | None
    contextWindowTotal: int | None
    launch: ConversationRunLaunch
