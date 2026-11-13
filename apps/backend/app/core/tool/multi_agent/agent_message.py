"""Agent 消息的入队前准备：委派与追加消息共用，产出待入队的 ``TaskMessage``。

和用户输入不同，Agent 文本是纯文本：不解析附件与图片 token，也不带图片。只校验发起配置与非空，
不写库、不入队、不创建 Run。
"""

from collections.abc import Sequence

from app.core.workflow.message_queue.task_message import AgentMessageBody, RunLaunch, TaskMessage
from app.models.conversation_input import MessageSource
from app.models.conversation_run_options import ConversationRunOptions, ReasoningEffort
from app.models.errors.input_errors import UserInputRejectedError
from app.service.depends import get_model_config_service


def prepare_agent_message(
    text: str,
    *,
    source: MessageSource,
    agent_id: str,
    model_config_id: str,
    reasoning_effort: ReasoningEffort | None = None,
    disabled_tool_groups: Sequence[str] = (),
) -> TaskMessage:
    """组装一条携带发起配置的 Agent 消息（尚未入队）；一律 ``immediate``：没有人替 Agent 点
    「立即发送」。

    ``disabled_tool_groups`` 是新 Run 关掉的工具组：Agent fork 沿用父 Run 的选择，委派不关。

    异常:
        UserInputRejectedError: 模型配置不存在、不支持所选推理强度，或正文为空。
    """
    get_model_config_service().check_launch_config(model_config_id, reasoning_effort)
    if not text.strip():
        raise UserInputRejectedError("EMPTY_INPUT", "Agent 消息正文为空")
    body = AgentMessageBody(
        text=text,
        source=source,
        launch=RunLaunch(
            agent_id=agent_id,
            model_config_id=model_config_id,
            options=ConversationRunOptions(
                reasoning_effort=reasoning_effort,
                disabled_tool_groups=list(disabled_tool_groups),
            ),
        ),
    )
    return TaskMessage.of(body)
