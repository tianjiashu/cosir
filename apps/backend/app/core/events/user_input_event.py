"""用户输入已并入模型上下文的事实事件。"""

from typing import Literal

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope


class UserInputConsumedEvent(WorkflowEventEnvelope):
    """一条外部队列中的用户输入已原子转入 canonical context，成为该 Run 的 user 消息。

    只带消息身份：展示 parts 已在排队投影里按展示绑定构造，Transport 投影按身份把它搬进 Run，
    执行内核不感知 wire schema。按 ``message_id`` 可重复投递。
    """

    type: Literal["user_input_consumed"] = "user_input_consumed"
    message_id: int
