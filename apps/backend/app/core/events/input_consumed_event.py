"""开启回合的输入（用户输入或 Agent 消息）已并入模型上下文的事实事件。"""

from typing import Literal

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope
from app.models.conversation_input import MessageSource, UserInputPresentation


class InputConsumedEvent(WorkflowEventEnvelope):
    """一条外部队列中的回合输入已原子转入 canonical context，成为该 Run 的 user 消息。

    带上确认删除那一刻的展示绑定：Transport 投影据此自己构造 user 消息，不依赖排队投影里
    还留着这一条（排队投影只镜像队列表，确认删除后随时可能被刷掉）。展示绑定是领域模型，
    执行内核仍不感知 wire schema。``source`` 非空即 Agent 消息（正文是纯文本，不按胶囊投影），
    为空即用户输入。按 ``message_id`` 可重复投递。``turn`` 是这一行写入时记下的回合号，与冷重建
    读行上的回合号同源：实时投影按它找到这一回合的回复——已有回复说明是途中来信（并入回复），
    没有就是这条输入开启了回合。
    """

    type: Literal["input_consumed"] = "input_consumed"
    message_id: int
    presentation: UserInputPresentation
    turn: int
    source: MessageSource | None = None
