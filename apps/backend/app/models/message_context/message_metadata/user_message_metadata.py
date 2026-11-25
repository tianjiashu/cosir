"""落库 user 行的附属信息：来源种类、展示绑定、调用归属与合成通知。"""

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.models.message_context.conversation_input import MessageSource
from app.models.message_context.message_metadata.base.message_kinds import (
    CALL_RESULT_KINDS,
    TURN_INPUT_KINDS,
    MessageKind,
)
from app.models.message_context.message_metadata.base.message_metadata_base import MessageMetadata
from app.models.message_context.message_metadata.system_message_metadata import PromptSourceBaseline
from app.models.terminal_permissions import TerminalPermissionMode


class TerminalPermissionBaseline(BaseModel):
    """终端权限通知告知时的有效权限与可写根：任一变化就再告知一次。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: TerminalPermissionMode
    writable_root: str


class NoticeEntry(BaseModel):
    """合成进一条 ``<system_notice>`` 的单条通知。字段只在这里定义一次：队列里的 ``NoticeBody`` 继承它，
    合成通知行时按本类的字段原样取出，不再逐个手抄。

    撤回回合时据此重新投递 Session 级通知；四个比对基准各只属于一种 Run 级通知，下一次推导取上下文里
    最近一条同类通知的那个字段对照（见 ``NOTICE_LEVELS``）。
    """

    model_config = ConfigDict(extra="forbid")
    notice_kind: str
    content: str
    # 这条通知给哪些调用送达了结局（提问作废、重启中断、派生 Session 被停止）：分叉与撤回据此按调用
    # 归属处理它；与具体调用无关的通知为空。
    tool_call_ids: list[str] = Field(default_factory=list)
    # 工具准入通知：告知时禁用的工具名（排序）。
    banned_tools: list[str] | None = None
    # 未了结事项通知：告知时的清单（排序）。
    open_items: list[str] | None = None
    # 配置变化通知：告知后 Agent 看到的全部配置指纹（见 ``SystemMessageMetadata``）。
    prompt_sources: PromptSourceBaseline | None = None
    # 读过的文件变化通知：告知时全部「读过但已变化」的文件，不透明，只有文件系统解读
    # （``app/tools/file_system/read_changes``）。
    stale_reads: list[dict[str, JsonValue]] | None = None
    # 终端权限通知：告知时的有效权限与可写根（见 ``notices.request_notices``）。
    terminal_permission: TerminalPermissionBaseline | None = None


class UserInputPresentation(BaseModel):
    """随 user 消息持久化的展示绑定；不重复保存 SDK 消息正文或 Run 执行配置。

    正文与模型看到的是同一份规范化胶囊文本（``<file>`` / ``<ref>`` / ``<skill>`` 指针，Skill
    已补上 ``path``），不再分开保存两份正文。``image_paths`` 是已上传图片的 workspace 相对路径，
    顺序即发送时的图片顺序。

    「胶囊指针可用」由入队边界 ``UserInputService`` 保证：那里 stat 本机文件、校验工作区引用、
    解析 Skill 并写回路径。本模型只承载已成立的事实，不再重复校验自身——它在每次快照重建与
    context 读取时都会被 ``model_validate``，为上游已保证的数据反复付出代价没有收益。

    ``client_message_id`` 是发送这条输入的客户端给它起的身份（前端乐观消息的 id）：排队、并入与冷重建
    三处投影原样带回，前端按它把乐观消息换成正式消息，不按「新出现了几条」推断。后端不解读、不校验
    唯一性；只有用户从界面发送的输入才有，模型看不到它。
    """

    model_config = ConfigDict(extra="forbid")
    text: str
    image_paths: list[str] = Field(default_factory=list)
    client_message_id: str | None = None


class UserMessageMetadata(MessageMetadata):
    """一条落库 user 行的附属信息：``kind`` 记录来源，其余字段按种类出现（见 :meth:`_fields_follow_kind`）。

    所有这些消息进模型时都是 user 角色，区分来源只能靠 ``kind``，不解析正文标签：编辑资格、排队与
    消费投影都据此判断。除工具图片（observe 节点直接写入）外都来自 Session 消息队列。内部注入的
    user 指令（如结构化输出）只拼进请求副本、不落库。

    按种类出现的字段：

    - ``presentation``：开启回合的输入（见 ``TURN_INPUT_KINDS``）。模型正文与展示正文是同一份规范化
      胶囊文本（Skill 已补 ``path``），图片路径单独保存在这里；snapshot 重建、编辑复用与 fork 都只从它取。
      Agent 消息（``agent_message``）只有纯文本正文，另带发送方 ``source``。
    - ``tool_call_id``：回答某次工具调用的行（见 ``CALL_RESULT_KINDS``）。决定与后台结论可能在发起调用
      之后的回合才并入，撤回据它判断这行回答的是哪次调用（见 ``turn_withdrawal_service``）。
    - ``notices``：合成通知行（``notice``）里的每条通知。分叉交接、后端重启这类 Session 级通知只投递
      一次，所在的回合被撤回时要据此重新投递，否则之后的 Run 永远看不到。
    """

    kind: MessageKind
    presentation: UserInputPresentation | None = None
    source: MessageSource | None = None
    tool_call_id: str | None = None
    notices: list[NoticeEntry] = Field(default_factory=list)

    @property
    def answered_calls(self) -> frozenset[str]:
        """这一行给哪些调用送达了结局：调用结果类消息的 ``tool_call_id``，合成通知行里各通知的所指调用。

        与队列里的 ``EnqueuedMessageBody.answered_calls`` 同一口径：分叉判断「结局在不在复制范围内」、
        撤回判断「回答的是不是被撤回的调用」都只认它。
        """
        calls = {call for notice in self.notices for call in notice.tool_call_ids}
        if self.tool_call_id is not None:
            calls.add(self.tool_call_id)
        return frozenset(calls)

    @model_validator(mode="after")
    def _fields_follow_kind(self) -> "UserMessageMetadata":
        """持久化与构造边界守住「哪种行带哪些字段」，读取方据 ``kind`` 直接取用、不再判空。"""
        if (self.kind in TURN_INPUT_KINDS) != (self.presentation is not None):
            raise ValueError("presentation is required for turn inputs and forbidden otherwise")
        if (self.kind == "agent_message") != (self.source is not None):
            raise ValueError("source is required for agent_message and forbidden otherwise")
        if (self.kind in CALL_RESULT_KINDS) != (self.tool_call_id is not None):
            raise ValueError("tool_call_id is required for call results and forbidden otherwise")
        if self.notices and self.kind != "notice":
            raise ValueError("notices are only allowed for notice messages")
        return self
