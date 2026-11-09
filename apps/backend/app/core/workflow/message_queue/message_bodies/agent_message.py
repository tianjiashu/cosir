"""Agent 消息：另一个 Agent 交给本 Task 的输入。

委派、追加消息、Team 节点输入，以及下级中途发来的消息。
"""

from typing import ClassVar

from openai.types.chat import ChatCompletionUserMessageParam

from app.core.workflow.message_queue.message_bodies.base import EnqueuedMessageBody, TaskMessageKind
from app.core.workflow.message_queue.message_bodies.rendering import escape_tag, render_time
from app.core.workflow.message_queue.message_bodies.user_input import RunLaunch
from app.models.conversation_input import MessageSource, UserInputPresentation
from app.models.message_metadata import UserMessageMetadata


class AgentMessageBody(EnqueuedMessageBody):
    """另一个 Agent 发来的消息：和用户输入一样开启一轮、可交接新 Run，但不是用户说的话。

    为什么不复用 ``user_input``：模型会把它读成用户的请求，界面会画成用户气泡，撤回也会把它
    当作「最新用户消息」。必须带 ``launch``：委派刚建的子 Task 还没有 Run，无从继承配置；
    ``launch`` 只在交接新 Run 时生效，并入运行中的 Run 或唤醒挂起的 Run 时不看它。正文是纯文本：
    不像用户输入那样校验胶囊指针，Agent 文本里恰好出现形如标签的内容也只是文字。
    """

    KIND: ClassVar[TaskMessageKind] = "agent_message"
    TAG: ClassVar[str] = "agent_message"
    PROMPT_GUIDE: ClassVar[str] = (
        "a message from another agent, named by from and source_task_id. For a delegated or "
        "forked assignment, the original caller receives the task's conclusion. For ordinary "
        "messages, send a reply explicitly with send_message to source_task_id if needed."
    )
    PRIORITY: ClassVar[int] = 1
    DEFAULT_PROJECTED: ClassVar[bool] = True
    WAKES: ClassVar[bool] = True
    text: str
    launch: RunLaunch
    source: MessageSource

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        sender = self.source.name.replace('"', "'")
        content = (
            f'<{self.TAG} time="{render_time()}" source_task_id="{self.source.task_id}" '
            f'from="{sender}">\n{escape_tag(self.text, self.TAG)}\n</{self.TAG}>'
        )
        return (
            ChatCompletionUserMessageParam(role="user", content=content),
            UserMessageMetadata(
                kind="agent_message",
                presentation=UserInputPresentation(text=self.text),
                source=self.source,
            ),
        )
