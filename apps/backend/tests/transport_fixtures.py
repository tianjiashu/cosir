"""Transport 投影测试共用的用户输入构造。

生产路径里用户输入先由调度 actor 刷新排队投影（``queued_messages`` 里已按展示绑定构造好 parts），
``prepare_input`` 并入后才发只带消息身份的 ``UserInputConsumedEvent``，投影按身份把排队条目搬进
Run。内存 snapshot 测试必须按同样的顺序准备状态，否则消费事件找不到排队条目。
"""

from __future__ import annotations

from typing import Any

from app.assistant_transport.projection.user_input_projection import build_user_input_parts
from app.core.events import UserInputConsumedEvent
from app.models.conversation_input import UserInputPresentation
from app.core.workflow.inbox.task_message import Delivery


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


def consumed(task_id: int, run_id: int, message_id: int) -> UserInputConsumedEvent:
    return UserInputConsumedEvent(task_id=task_id, run_id=run_id, message_id=message_id)
