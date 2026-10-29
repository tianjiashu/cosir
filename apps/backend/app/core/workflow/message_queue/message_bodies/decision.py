"""用户对等待决定的工具调用（HITL）作出的决定。"""

from typing import Any, ClassVar, Literal

from openai.types.chat import ChatCompletionUserMessageParam

from app.core.workflow.message_queue.message_bodies.base import EnqueuedMessageBody, TaskMessageKind
from app.core.workflow.message_queue.message_bodies.rendering import escape_tag, render_time
from app.models.message_metadata import UserMessageMetadata


class DecisionBody(EnqueuedMessageBody):
    """用户对一次等待决定的工具调用（HITL）作出的决定。

    去向由工具声明（``ToolDefinition.user_decision``），不看决定本身：``execute`` 类工具无论批准还是
    驳回，``prepare_input`` 都绕过模型、带着决定直接执行 ``tool_call_id`` 对应的原调用（参数从
    context 里的原调用取，不随消息保存）——驳回同样要落定工具自己的事实，也只有执行结果能让等待
    占位收尾；``reply`` 类工具的决定作为消息交给模型。一轮只处理一条需要执行的决定，且先于其他
    消息处理。用户主动作出的决定可以唤醒挂起的 Run，也能在 Run 已完成、取消或失败后开出新 Run。

    ``data`` 是工具自定义的决定载荷（如 Agent Team 批准时编辑过的方案），形状由工具在入队边界
    校验、执行时消费；不进模型正文。
    """

    KIND: ClassVar[TaskMessageKind] = "user_decision"
    TAG: ClassVar[str] = "user_decision"
    PROMPT_GUIDE: ClassVar[str] = (
        "the user's approve/reject on a tool call that was waiting for their decision, matched "
        "by `tool_call_id`. `feedback`, if present, is the user's own words about that call."
    )
    PRIORITY: ClassVar[int] = 1
    DEFAULT_PROJECTED: ClassVar[bool] = False
    WAKES: ClassVar[bool] = True
    tool_call_id: str
    tool_name: str
    decision: Literal["approve", "reject"]
    feedback: str | None = None
    data: dict[str, Any] | None = None

    @property
    def approved(self) -> bool:
        return self.decision == "approve"

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        fb = ""
        if self.feedback:
            fb = f"\nfeedback: {escape_tag(self.feedback, self.TAG)}"
        content = (
            f'<{self.TAG} time="{render_time()}" tool_call_id="{self.tool_call_id}" '
            f'tool_name="{self.tool_name}">'
            f"\ndecision: {self.decision}{fb}\n</{self.TAG}>"
        )
        return (
            ChatCompletionUserMessageParam(role="user", content=content),
            UserMessageMetadata(kind="decision", tool_call_id=self.tool_call_id),
        )
