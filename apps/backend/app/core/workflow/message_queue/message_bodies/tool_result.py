"""用户决定后 HITL 调用的执行结果：Run 内直接写入 context，不入队。

因此它不继承 :class:`EnqueuedMessageBody`：没有 ``KIND``、不进外部队列、也没有推进语义。
"""

from typing import ClassVar, Literal

from openai.types.chat import ChatCompletionUserMessageParam
from pydantic import BaseModel, ConfigDict

from app.core.workflow.message_queue.message_bodies.rendering import escape_tag
from app.models.message_metadata import UserMessageMetadata


class ToolResultBody(BaseModel):
    """用户决定后 HITL 调用的执行结果。

    原调用的 tool 消息已是等待占位，决定可能在之后的模型步甚至之后的 Run 才到，结果只能追加为
    user 消息，不覆盖占位（覆盖会让历史前缀分叉）；写入与卡片收尾见
    ``settle_decision_observation``。
    """

    model_config = ConfigDict(extra="forbid")
    TAG: ClassVar[str] = "tool_result"
    PROMPT_GUIDE: ClassVar[str] = (
        "the late result of an earlier tool call that was waiting for the user's decision; it "
        "supersedes that call's placeholder result."
    )
    tool_call_id: str
    tool_name: str
    status: Literal["success", "error", "cancelled"]
    content: str

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        content = (
            f'<{self.TAG} tool_call_id="{self.tool_call_id}" tool_name="{self.tool_name}" '
            f'status="{self.status}">\n{escape_tag(self.content, self.TAG)}\n</{self.TAG}>'
        )
        return (
            ChatCompletionUserMessageParam(role="user", content=content),
            UserMessageMetadata(kind="tool_result"),
        )
