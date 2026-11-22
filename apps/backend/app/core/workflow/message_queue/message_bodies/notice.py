"""系统通知消息：外部队列行（分叉交接、后端重启等），或驱动内通知（``RunMailbox.notify``，不入队）。"""

from collections.abc import Sequence
from typing import ClassVar, Literal

from openai.types.chat import ChatCompletionUserMessageParam
from pydantic import Field

from app.core.workflow.message_queue.message_bodies.base import (
    EnqueuedMessageBody,
    SessionMessageKind,
    WakeScope,
)
from app.core.workflow.message_queue.message_bodies.rendering import render_time
from app.models.message_context.message_metadata.user_message_metadata import (
    NoticeEntry,
    UserMessageMetadata,
)

NoticeKind = Literal[
    "fork_handover",
    "backend_restarted",
    "derived_session_stopped",
    "tool_availability",
    "user_cancelled",
    "model_continuation",
    "decision_expired",
    "open_items",
    "configuration_changed",
    "terminal_file_changes",
    "terminal_permissions",
    "read_files_changed",
]
NoticeLevel = Literal["session", "run", "turn"]

# 通知分级：级别决定通知从哪来、存在哪、丢了或所在回合被撤回时怎么办。分界只看一个问题——
# 这件事能不能事后从持久化事实与上下文里重新算出来：
# - session：发生过一次的事件（分叉交接、后端重启、派生 Session 被用户停止或删除、提问作废）。事后推不出
#   「模型还没被告知」，所以由事件发生处经外部队列投一次，确认与写入 context 同一事务、不会丢；所在
#   回合被撤回时重新投递（``turn_withdrawal_service``），否则之后的 Run 永远看不到。
# - run：此刻成立的状态（工具准入、还有哪些事没了结、Agent 看到的配置）。不入队，经 ``mailbox.notify``
#   投、随 mailbox 重置丢弃；比对基准是上下文里最近一条同类通知（配置没有通知时看 seq 0 系统行），写入
#   后才成为新基准，所以写入前的任何中断与撤回都只会让下次重推，不会漏告。会在驱动中途变的每次模型请求
#   前对账（配置：``RuntimeMessageContextManager.configuration_change_notice``；读过的文件：
#   ``read_change_notice``），其余在驱动开始时推导（``drive_notices``）。
# - turn：上一回合怎样结束（用户已停止，``drive_notices``）或驱动内的一次性提示（输出被截断时的续写，由
#   model 节点当场投）。同 run 级，不入队、可重推或丢了无妨。
# 新增通知按此归类：推不出来 → session，经 ``session_runtime_spaces.enqueue`` 入队；推得出来 → 驱动
# 中途会变且下一步就该知道，进每次请求前的对账，否则加进 ``drive_notices``；基准一律取上下文，不放进程内记忆。
# 例外：``decision_expired`` 在 Run 内作废时走 mailbox（``prepare_input`` 先提交作废、再与本批通知一起
# 写入，两步不同事务，中间崩溃会漏告，已接受）；Run 落定后作废才经外部队列。
#
# 送达占位结局的通知必须带所指调用（``tool_call_ids``）：提问作废、重启中断把占位原地改成「已取消 /
# 已中断」、正文不动，结局只在这条通知里；派生 Session 被停止说的是派生它的那次调用。占位的结局总是
# 一条按调用寻址的消息（后台结论、决定同理），分叉与撤回都按调用归属处理它——不带所指调用，分支会
# 继承「等待中」的占位而收不到结局，撤回会留下指向已撤回调用的通知。
NOTICE_LEVELS: dict[NoticeKind, NoticeLevel] = {
    "fork_handover": "session",
    "backend_restarted": "session",
    "derived_session_stopped": "session",
    "tool_availability": "run",
    "open_items": "run",
    "configuration_changed": "run",
    "terminal_file_changes": "session",
    "terminal_permissions": "run",
    "read_files_changed": "run",
    "user_cancelled": "turn",
    "model_continuation": "turn",
    # 所在回合被撤回时重新投递：提问仍是作废的，模型还得知道。
    "decision_expired": "session",
}

# 通知的唤醒范围（见 ``WakeScope``）：通知多半只提醒、不值得单独跑一轮模型。例外是「等着的东西没了」：
# 派生的 Session 被用户停止或删除、后端重启都不会再汇报，挂起在 ``wait_messages`` 上等它们的
# Agent 只能靠这条通知醒来，否则一直挂着；Agent 空闲时仍不为它开新 Run。
NOTICE_WAKE_SCOPES: dict[NoticeKind, WakeScope] = {
    "fork_handover": "none",
    "backend_restarted": "active",
    "derived_session_stopped": "active",
    "tool_availability": "none",
    "open_items": "none",
    "configuration_changed": "none",
    "terminal_file_changes": "none",
    "terminal_permissions": "none",
    "read_files_changed": "none",
    "user_cancelled": "none",
    "model_continuation": "none",
    "decision_expired": "none",
}


class NoticeBody(EnqueuedMessageBody, NoticeEntry):
    """系统通知；不启动 Run、不投影。

    ``content`` 保持原始文本，``<system_notice>`` 包装由 :func:`notices_to_context` 统一加；
    「静默纳入、不要回复」的读法由 ``PROMPT_GUIDE`` 声明（「不是用户的话」是消息协议前言的通用
    规则）、只进系统提示词，不随每条通知重复；读法里不列举通知种类，「这是什么」由每条通知的正文开头自己
    说明，新增种类不必回来改读法；只为配置变化多写一句行为规则：它带来的是指令（替换提示词里对应的
    部分），而前言把系统消息归为「待评估的信息」，不点明 Agent 会只当参考。``notice_kind`` /
    ``metadata`` 只服务观测，不进模型正文。字段定义（含四个比对基准）继承自落库的 ``NoticeEntry``，
    这里只收窄 ``notice_kind`` 并加上只属于队列的 ``metadata``。
    """

    KIND: ClassVar[SessionMessageKind] = "notice"
    TAG: ClassVar[str] = "system_notice"
    PROMPT_GUIDE: ClassVar[str | None] = (
        "runtime facts from the system: take them into account silently; never reply to, "
        "acknowledge, or quote them. A configuration change replaces the matching part of your "
        "instructions."
    )
    DEFAULT_PROJECTED: ClassVar[bool] = False
    # 通知先于其余消息写入：模型先看到环境变化，再看用户说的话。
    PRIORITY: ClassVar[int] = 0
    # 类型层面不推进（启动调度不为它拉起 actor）；按通知种类细分见 ``wake_scope``。
    WAKE: ClassVar[WakeScope] = "none"
    notice_kind: NoticeKind
    metadata: dict[str, str | None] = Field(default_factory=dict)

    def wake_scope(self) -> WakeScope:
        return NOTICE_WAKE_SCOPES[self.notice_kind]

    def answered_calls(self) -> frozenset[str]:
        return frozenset(self.tool_call_ids)

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        return notices_to_context([self])

    def entry(self) -> NoticeEntry:
        """落库用的那份：只取 ``NoticeEntry`` 自己的字段，队列专属的 ``metadata`` 不进合成通知行。

        不能用 ``NoticeEntry.model_validate(self)``：子类实例会原样返回，连同 ``metadata`` 一起落库。
        """
        return NoticeEntry.model_validate(self.model_dump(include=set(NoticeEntry.model_fields)))


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
            notices=[item.entry() for item in notices],
        ),
    )
