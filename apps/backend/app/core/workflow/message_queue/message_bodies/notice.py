"""系统通知消息：外部队列行（配置广播、分叉交接等），或驱动内通知（``RunMailbox.notify``，不入队）。"""

from collections.abc import Sequence
from typing import ClassVar, Literal

from openai.types.chat import ChatCompletionUserMessageParam
from pydantic import Field

from app.core.workflow.message_queue.message_bodies.base import (
    EnqueuedMessageBody,
    TaskMessageKind,
    WakeScope,
)
from app.core.workflow.message_queue.message_bodies.rendering import render_time
from app.models.message_metadata import NoticeEntry, UserMessageMetadata

NoticeKind = Literal[
    "system_prompt_delta",
    "catalog_change",
    "fork_handover",
    "backend_restarted",
    "derived_task_stopped",
    "tool_availability",
    "user_cancelled",
    "model_continuation",
]
NoticeLevel = Literal["task", "run", "turn"]

# 通知按它说的事属于哪一层分级，级别决定通知从哪来、撤回回合时怎么办：
# - task：Task 环境的变化（系统提示词、能力目录、分叉交接、后端重启、派生 Task 被用户停止）。事件只在那一刻
#   存在，经外部队列投递、只投一次；所在回合被撤回时重新投递，否则之后的 Run 永远看不到这次变化。
# - run：本 Run 的事实（工具准入）。驱动开始时对照上下文推导（``drive_notices``），不入队。
# - turn：上一个回合怎样结束（用户已停止）或本次驱动内的提示（续写）。驱动开始时推导或驱动内投递，
#   不入队。
# run 与 turn 级都从持久化事实与上下文现推，撤回后自然重新推导，所以撤回只需重新投递 task 级。
NOTICE_LEVELS: dict[NoticeKind, NoticeLevel] = {
    "system_prompt_delta": "task",
    "catalog_change": "task",
    "fork_handover": "task",
    "backend_restarted": "task",
    "derived_task_stopped": "task",
    "tool_availability": "run",
    "user_cancelled": "turn",
    "model_continuation": "turn",
}

# 通知的唤醒范围（见 ``WakeScope``）：通知多半只提醒、不值得单独跑一轮模型。例外是「等着的东西没了」：
# 派生的 Task 被用户停止或删除、后端重启都不会再汇报，挂起在 ``wait_messages`` 上等它们的
# Agent 只能靠这条通知醒来，否则一直挂着；Agent 空闲时仍不为它开新 Run。
NOTICE_WAKE_SCOPES: dict[NoticeKind, WakeScope] = {
    "system_prompt_delta": "none",
    "catalog_change": "none",
    "fork_handover": "none",
    "backend_restarted": "active",
    "derived_task_stopped": "active",
    "tool_availability": "none",
    "user_cancelled": "none",
    "model_continuation": "none",
}


class NoticeBody(EnqueuedMessageBody):
    """系统通知；不启动 Run、不投影。

    ``content`` 保持原始文本，``<system_notice>`` 包装由 :func:`notices_to_context` 统一加；
    「静默纳入、不要回复」的读法由 ``PROMPT_GUIDE`` 声明（「不是用户的话」是消息协议前言的通用
    规则）、只进系统提示词，不随每条通知重复。``notice_kind`` / ``metadata`` 只服务观测，不进模型正文。
    """

    KIND: ClassVar[TaskMessageKind] = "system_notice"
    TAG: ClassVar[str] = "system_notice"
    PROMPT_GUIDE: ClassVar[str | None] = (
        "runtime facts injected by the system (instruction or catalog changes, tool admission, "
        "continuation hints): take them into account silently; never reply "
        "to, acknowledge, or quote them."
    )
    DEFAULT_PROJECTED: ClassVar[bool] = False
    # 通知先于其余消息写入：模型先看到环境变化，再看用户说的话。
    PRIORITY: ClassVar[int] = 0
    # 类型层面不推进（启动调度不为它拉起 actor）；按通知种类细分见 ``wake_scope``。
    WAKE: ClassVar[WakeScope] = "none"
    content: str
    notice_kind: NoticeKind
    metadata: dict[str, str | None] = Field(default_factory=dict)
    # 只有工具准入通知带：告知时禁用的工具名（排序），是下一次推导的比对基准（随行落库）。
    banned_tools: list[str] | None = None

    def wake_scope(self) -> WakeScope:
        return NOTICE_WAKE_SCOPES[self.notice_kind]

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        return notices_to_context([self])


def notices_to_context(
        notices: Sequence[NoticeBody],
) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
    """把一批通知合成一条带 ``<system_notice>`` 标签的 user 消息。

    用 user 而非 system 角色：对话中途的 system 消息在部分供应商 / 兼容层不被接受。prepare_input
    每次请求前把 mailbox 里的全部通知（两种来源）一次取光、经这里合成一条：减少 context 条数；
    落库后成为历史固定行，后续请求前缀不分叉。
    """
    lines = "".join(f"\n{item.content}" for item in notices)
    tag = NoticeBody.TAG
    content = f'<{tag} time="{render_time()}">{lines}\n</{tag}>'

    return (
        ChatCompletionUserMessageParam(role="user", content=content),
        UserMessageMetadata(
            kind="notice",
            notices=[
                NoticeEntry(
                    notice_kind=item.notice_kind,
                    content=item.content,
                    banned_tools=item.banned_tools,
                )
                for item in notices
            ],
        ),
    )
