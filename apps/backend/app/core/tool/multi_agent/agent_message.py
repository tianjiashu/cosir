"""Agent 消息的入队前准备：委派与追加消息共用，产出待入队的 ``SessionMessage``。

和用户输入不同，Agent 文本是纯文本：不解析附件与图片 token，也不带图片。只校验发起配置与非空，
不写库、不入队、不创建 Run。
"""

from app.core.workflow.message_queue.session_message import (
    AgentMessageBody,
    RunLaunch,
    SessionMessage,
)
from app.models.errors.input_errors import UserInputRejectedError
from app.models.message_context.conversation_input import MessageSource
from app.service.depends import get_model_config_service


def prepare_agent_message(text: str, *, source: MessageSource, launch: RunLaunch) -> SessionMessage:
    """组装一条携带发起配置的 Agent 消息（尚未入队）；一律 ``immediate``：没有人替 Agent 点
    「立即发送」。发起配置由调用方给全（多数直接沿用目标会话现成的 ``launch``），这里不补默认值。

    异常:
        UserInputRejectedError: 模型配置不存在、不支持所选推理强度，或正文为空。
    """
    get_model_config_service().check_launch_config(launch.model_config_id, launch.reasoning_effort)
    if not text.strip():
        raise UserInputRejectedError("EMPTY_INPUT", "Agent 消息正文为空")
    return SessionMessage.of(AgentMessageBody(text=text, source=source, launch=launch))
