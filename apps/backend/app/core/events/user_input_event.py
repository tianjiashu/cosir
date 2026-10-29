"""用户输入已并入模型上下文的事实事件。"""

from typing import Literal

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope
from app.models.conversation_input import UserInputPresentation


class UserInputConsumedEvent(WorkflowEventEnvelope):
    """一条外部队列中的用户输入已原子转入 canonical context，成为该 Run 的 user 消息。

    带上确认删除那一刻的展示绑定：Transport 投影据此自己构造 user 消息，不依赖排队投影里
    还留着这一条（排队投影只镜像队列表，确认删除后随时可能被刷掉）。展示绑定是领域模型，
    执行内核仍不感知 wire schema。按 ``message_id`` 可重复投递。
    """

    type: Literal["user_input_consumed"] = "user_input_consumed"
    message_id: int
    presentation: UserInputPresentation
