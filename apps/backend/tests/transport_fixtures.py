"""Transport 投影测试共用的用户输入构造。

生产路径里用户输入先进外部队列（排队投影镜像它），``prepare_input`` 并入后发带展示绑定的
``UserInputConsumedEvent``，投影据此构造 user 消息并把它移出排队投影。消费投影不依赖排队条目，
``queue_user_input`` 只在要验证「移出排队投影」时使用。
"""

from __future__ import annotations

from typing import Any

from app.assistant_transport.projection.user_input_projection import build_user_input_parts
from app.core.events import UserInputConsumedEvent
from app.models.conversation_input import UserInputPresentation
from app.core.workflow.message_queue.task_message import Delivery


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
            "parts": build_user_input_parts(
                presentation.display_text, presentation.image_paths, presentation.attachments
            ),
            "delivery": delivery,
        }
    )


def consumed(
    task_id: int, run_id: int, message_id: int, presentation: UserInputPresentation
) -> UserInputConsumedEvent:
    return UserInputConsumedEvent(
        task_id=task_id, run_id=run_id, message_id=message_id, presentation=presentation
    )
