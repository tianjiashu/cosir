"""外部持久化队列与 Run 内投递通道共用的队列消息实体。

本文件只承载信封：``Delivery``（并入时机）与 ``TaskMessage``（按 body 类型补齐 ``kind`` 与投影
默认值，并在持久化边界按 ``kind`` 列还原 body）。各类消息的正文、标签、读法以及「如何变成
context 消息」的渲染规则在 ``message_bodies``；那些类型由本模块原路重导出，调用方仍从
``task_message`` 取用。
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from app.core.workflow.message_queue.message_bodies import (
    _BODY_OF_KIND,
    LAUNCH_INPUT_BODIES,
    MODEL_INPUT_BODIES,
    AgentMessageBody,
    BackgroundResultBody,
    DecisionBody,
    LaunchInputBody,
    MessageBody,
    NoticeBody,
    RunLaunch,
    TaskMessageKind,
    ToolImageBody,
    ToolResultBody,
    UserInputBody,
    escape_tag,
    notices_to_context,
)

if TYPE_CHECKING:
    from app.storage.model.task_message_model import TaskMessageModel

Delivery = Literal["immediate", "next_run"]
"""并入时机：``immediate`` 在当前执行的下一次模型请求前并入；``next_run`` 只交给下一个 Run。

用户输入还据此分通道：``immediate`` 优先被新 Run 选中，``next_run`` 排队、一个 Run 只消费一条
（见 ``dispatch_policy.next_input``）。
"""

__all__ = [
    "LAUNCH_INPUT_BODIES",
    "MODEL_INPUT_BODIES",
    "AgentMessageBody",
    "BackgroundResultBody",
    "DecisionBody",
    "Delivery",
    "LaunchInputBody",
    "MessageBody",
    "NoticeBody",
    "RunLaunch",
    "TaskMessage",
    "TaskMessageKind",
    "ToolImageBody",
    "ToolResultBody",
    "UserInputBody",
    "escape_tag",
    "notices_to_context",
]


@dataclass(frozen=True)
class TaskMessage:
    """一条队列消息；``id`` 是外部队列行 id，也是排队与已消费阶段共用的消息身份。

    ``id`` 为 ``None`` 只有两种情形：入队前，以及执行内通知（``RunMailbox.notify``，不落库、不需要
    确认）。
    ``kind`` 由 body 类型决定（经 :meth:`of` 构造），持久化时单独成列，读回时按它还原 body。
    """

    id: int | None
    kind: TaskMessageKind
    body: MessageBody
    delivery: Delivery = "immediate"
    project_to_transport: bool = True

    def wakes(self, run_id: int | None) -> bool:
        """可否推进指定的活动 Run（驱动中不挂起、挂起中唤醒）；None 表示可否凭此消息新建 Run。

        只读消息体声明的唤醒范围（``wake_scope``），信封不认识任何具体类型。
        """
        scope = self.body.wake_scope()
        return scope == "new_or_active" or (scope == "active" and run_id is not None)

    @classmethod
    def of(
        cls,
        body: MessageBody,
        *,
        delivery: Delivery = "immediate",
        project_to_transport: bool | None = None,
        message_id: int | None = None,
    ) -> "TaskMessage":
        """按 body 类型补齐 ``kind`` 与投影默认值。"""
        if project_to_transport is None:
            project_to_transport = type(body).DEFAULT_PROJECTED
        return cls(
            id=message_id,
            kind=type(body).KIND,
            body=body,
            delivery=delivery,
            project_to_transport=project_to_transport,
        )

    @classmethod
    def of_model(cls, row: "TaskMessageModel") -> "TaskMessage":
        """持久化边界：按 ``kind`` 列校验并还原 body；未知 kind 或形状不符直接失败。"""
        body = _BODY_OF_KIND[row.kind].model_validate(row.payload)
        return cls(
            id=row.id,
            kind=row.kind,
            body=body,
            delivery=row.delivery,
            project_to_transport=row.project_to_transport,
        )
