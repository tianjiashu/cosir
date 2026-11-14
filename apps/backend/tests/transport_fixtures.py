"""Transport 投影测试共用的用户输入构造。

生产路径里用户输入先进外部队列（排队投影镜像它），``prepare_input`` 并入后发带展示绑定的
``InputConsumedEvent``，投影据此构造 user 消息并把它移出排队投影。消费投影不依赖排队条目，
``queue_user_input`` 只在要验证「移出排队投影」时使用。
"""

from __future__ import annotations

from typing import Any

from app.assistant_transport.projection.user_input_projection import build_presentation_parts
from app.core.events import InputConsumedEvent
from app.core.workflow.message_queue.session_message import Delivery
from app.models.conversation_input import UserInputPresentation


def queue_user_input(
    state: dict[str, Any],
    message_id: int,
    presentation: UserInputPresentation,
    delivery: Delivery = "immediate",
) -> None:
    """把一条用户输入按生产口径投影进 ``state["queued_messages"]``（wire id 是行 id 的字符串）。"""

    state.setdefault("queued_messages", []).append(
        {
            "id": str(message_id),
            "role": "user",
            "parts": build_presentation_parts(presentation),
            "delivery": delivery,
        }
    )


def consumed(
    session_id: int,
    run_id: int,
    message_id: int,
    presentation: UserInputPresentation,
    *,
    turn: int | None = None,
) -> InputConsumedEvent:
    """用户输入总开一个新回合：默认回合号取消息身份（夹具里每条输入一个回合）；途中来信由调用方
    传它所在回合的号。"""
    return InputConsumedEvent(
        session_id=session_id,
        run_id=run_id,
        message_id=message_id,
        presentation=presentation,
        turn=message_id if turn is None else turn,
    )
