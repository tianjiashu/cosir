"""系统通知消息：配置广播（持久化行）或 Run 内提示（只进 mailbox）。"""

from collections.abc import Sequence
from typing import ClassVar

from openai.types.chat import ChatCompletionUserMessageParam
from pydantic import Field

from app.core.workflow.message_queue.message_bodies.base import EnqueuedMessageBody, TaskMessageKind
from app.core.workflow.message_queue.message_bodies.rendering import render_time
from app.models.message_metadata import UserMessageMetadata


class NoticeBody(EnqueuedMessageBody):
    """系统通知；不启动 Run、不投影。

    ``content`` 保持原始文本，``<system_notice>`` 包装由 :func:`notices_to_context` 统一加；
    「通知不是用户输入、不要回复」等读法由 ``PROMPT_GUIDE`` 声明、只进系统提示词，不随每条
    通知重复。``notice_kind`` / ``metadata`` 只服务观测，不进模型正文。
    """

    KIND: ClassVar[TaskMessageKind] = "system_notice"
    TAG: ClassVar[str] = "system_notice"
    PROMPT_GUIDE: ClassVar[str] = (
        "runtime facts injected by the system (instruction or catalog changes, tool admission, "
        "continuation hints). Not from the user: take them into account silently; never reply "
        "to, acknowledge, or quote them."
    )
    DEFAULT_PROJECTED: ClassVar[bool] = False
    # 通知先于其余消息写入：模型先看到环境变化，再看用户说的话。
    PRIORITY: ClassVar[int] = 0
    # 例外：通知只提醒、不值得单独跑一轮模型，留在队列里随下一次推进一起写入。
    WAKES: ClassVar[bool] = False
    content: str
    notice_kind: str
    metadata: dict[str, str | None] = Field(default_factory=dict)

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        return notices_to_context([self])


def notices_to_context(
        notices: Sequence[NoticeBody],
) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
    """把一批通知合成一条带 ``<system_notice>`` 标签的 user 消息。

    用 user 而非 system 角色：对话中途的 system 消息在部分供应商 / 兼容层不被接受。合并而不是
    逐条写入：减少 context 条数；落库后成为历史固定行，后续请求前缀不分叉。
    """
    lines = "".join(f"\n{item.content}" for item in notices)
    tag = NoticeBody.TAG
    content = f'<{tag} time="{render_time()}">{lines}\n</{tag}>'

    return (
        ChatCompletionUserMessageParam(role="user", content=content),
        UserMessageMetadata(kind="notice"),
    )
