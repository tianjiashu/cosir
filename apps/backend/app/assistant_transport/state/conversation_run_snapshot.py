"""Assistant Transport 中单个 Run 的快照契约。"""

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_error import ConversationStateError
from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_usage import ConversationStateUsage
from app.models.conversation_run_options import ConversationRunOptions


class ConversationRunLaunch(TypedDict):
    """Run 冻结的发起配置；前端据此判断运行中发送能否立即并入（运行期配置锁定）。

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


class ConversationRunSnapshot(TypedDict):
    """一个 Run 的完整 UI 投影。

    ``runId`` 是内部消息、tool part 和 Run usage 的共同归属键；Run 的状态、终态原因、错误、
    消息及累计模型 usage 必须在同一对象内保持一致。

    ``error`` 只在 Run 进入失败/取消终态时携带 ``code`` + UI 展示 ``message``；模型 HTTP 失败
    时 ``message`` 可直接来自响应体。运行中、已结束成功或未发生错误时为 ``None``。它只服务
    UI 渲染与重连恢复，不是 Run 生命周期状态机的第二来源。
    """

    runId: int
    status: str
    endReason: str | None
    contextWindowTotal: int | None
    launch: ConversationRunLaunch
    messages: list[ConversationStateMessage]
    usage: ConversationStateUsage | None
    error: ConversationStateError | None
