"""Agent 消息：另一个 Agent 交给本 Session 的输入。

委派、追加消息、下级中途发来的消息，以及派生 Session 的汇报（来源带 ``report``）。
"""

from typing import ClassVar

from openai.types.chat import ChatCompletionUserMessageParam

from app.core.workflow.message_queue.message_bodies.base import (
    EnqueuedMessageBody,
    SessionMessageKind,
    WakeScope,
)
from app.core.workflow.message_queue.message_bodies.rendering import escape_tag, render_time
from app.core.workflow.message_queue.message_bodies.user_input import RunLaunch
from app.models.conversation_input import MessageSource, UserInputPresentation
from app.models.message_metadata import UserMessageMetadata


class AgentMessageBody(EnqueuedMessageBody):
    """另一个 Agent 发来的消息：和用户输入一样开启一轮、可交接新 Run，但不是用户说的话。

    为什么不复用 ``user_input``：模型会把它读成用户的请求，界面会画成用户气泡，撤回也会把它
    当作「最新用户消息」。必须带 ``launch``：委派刚建的子 Session 还没有 Run，无从继承配置；
    ``launch`` 只在交接新 Run 时生效，并入运行中的 Run 或唤醒挂起的 Run 时不看它。正文是纯文本：
    不像用户输入那样校验胶囊指针，Agent 文本里恰好出现形如标签的内容也只是文字。
    """

    KIND: ClassVar[SessionMessageKind] = "agent_message"
    TAG: ClassVar[str] = "agent_message"
    PROMPT_GUIDE: ClassVar[str | None] = (
        "a message from another agent, named by from and source_session_id. When it carries "
        "report=..., it is the report of a session you derived (a delegated child agent or a fork). "
        "status says how its run ended, report=\"intermediate\" means it still has work running "
        "and will report again, report=\"final\" means it has nothing left running. Derived sessions "
        "report to you automatically, so do not ask them to reply; for other messages, reply "
        "explicitly with send_message to source_session_id if needed."
    )
    PRIORITY: ClassVar[int] = 1
    DEFAULT_PROJECTED: ClassVar[bool] = True
    WAKE: ClassVar[WakeScope] = "new_or_active"
    text: str
    launch: RunLaunch
    source: MessageSource

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        sender = self.source.name.replace('"', "'")
        report = self.source.report
        attributes = (
            ""
            if report is None
            else (
                f' report="{"intermediate" if report.intermediate else "final"}"'
                f' status="{report.status}"'
            )
        )
        content = (
            f'<{self.TAG} time="{render_time()}" source_session_id="{self.source.session_id}" '
            f'from="{sender}"{attributes}>\n{escape_tag(self.text, self.TAG)}\n</{self.TAG}>'
        )
        return (
            ChatCompletionUserMessageParam(role="user", content=content),
            UserMessageMetadata(
                kind="agent_message",
                presentation=UserInputPresentation(text=self.text),
                source=self.source,
            ),
        )
