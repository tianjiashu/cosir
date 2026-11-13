"""Session context 的唯一运行时协调入口：SDK 负责模型聚合，本层负责 working copy、序号、回合与
协议闭合。

行级持久化（追加、原地替换、固定系统提示词）经 ``ContextStore``，本层不直接碰存储。每一行所属的
回合在这里写入时判定一次（:meth:`_opens_turn`），快照分组、实时投影与 fork 切点都读这个事实。
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Literal, cast

from openai.lib.streaming.chat import ParsedChatCompletionSnapshot
from openai.types.chat import (
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
    ParsedFunctionToolCall,
)
from sqlalchemy.orm import Session as DbSession

from app.config.logging.logger import log
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.context import SystemPromptBuilder
from app.core.context.context_entry import ContextEntry
from app.core.context.context_store import (
    SESSION_SYSTEM_PROMPT_SEQUENCE,
    ContextStore,
)
from app.core.context.tool_call_closure import (
    ContextProtocolError,
    ToolCallClosurePlan,
    build_placeholder_tool_message,
    plan_tool_call_closures,
)
from app.core.context.vision_input import resolve_messages_for_model
from app.core.llm_provider.message_codec import assistant_message
from app.models import (
    RunRecord,
    WorkspaceRecord,
)
from app.models.session_context import (
    SessionContextRecord,
)
from app.models.message_metadata import (
    TURN_INPUT_KINDS,
    AssistantMessageMetadata,
    MessageMetadata,
    NoticeEntry,
    ToolMessageMetadata,
    UserMessageMetadata,
    empty_message_metadata,
)
from app.utils.message_content import content_to_text


def _is_turn_input(
    message: ChatCompletionMessageParam, metadata: MessageMetadata, project: bool
) -> bool:
    """可见的一轮输入（用户输入或 Agent 消息）；隐藏的输入不在前端开回合，也就不算。"""
    return (
        message["role"] == "user"
        and isinstance(metadata, UserMessageMetadata)
        and metadata.kind in TURN_INPUT_KINDS
        and project
    )


def _is_visible_reply(message: ChatCompletionMessageParam) -> bool:
    """有正文或调用的 assistant 行：实时投影只在这时建出回复（见 ``ensure_reply``）。"""
    return message["role"] == "assistant" and bool(
        content_to_text(message.get("content")) or message.get("tool_calls")
    )


@dataclass
class RuntimeMessageContextManager:
    """一个 Session 的 Agent context 读写边界与内存消息列表 owner。

    内存消息列表（``_entries``）是模型请求的唯一来源，``load_message`` 只读它；每次
    :meth:`begin_run` 从数据库按 ``include_in_context`` 重建一次，撤回、fork 等改库用例因此
    不必通知或卸载本对象。列表与序号只经 :meth:`_track` 推进，且只接纳已写库的行。
    """

    current_session_id: int
    # 只用其身份与执行资源（角色、提示词）构建 Session 首个系统提示词，及供配置广播过滤。
    agent: AgentRuntimeConfig
    # Session 固化的工具名（``SessionRuntimeSpace.session_tool_names``），决定首个系统提示词的
    # 工具目录层与工作方式层。
    tool_names: tuple[str, ...]
    # 行级持久化边界：追加、原地替换、序号与 Session 固定系统提示词都经它读写。
    context_store: ContextStore
    # 工作区记录：系统提示词取 ``root_path``，模型请求的附件解析取 ``id``。必填——缺工作区
    # 会生成一份没有工作区事实的系统提示词并被持久化成 Session 固定前缀，属于不可逆的静默降级。
    workspace: WorkspaceRecord
    current_run_id: int | None = None
    # 固定持久化的 Session system prompt，不参与压缩；每次模型调用均位于消息前缀。
    _system_entry: ContextEntry | None = field(default=None, init=False)
    _entries: list[ContextEntry] = field(default_factory=list, init=False)
    # 从持久化 context 恢复序号；每条新消息在成功落库后占用一个序号。
    _message_sequence: int = field(default=0, init=False)
    # 当前模型调用正在流式写入的响应行身份（``include_in_context`` 仍为 False）。
    # 只用于「同一次调用持续替换同一行」；提交或开始下一次调用即清空。
    _draft_response: ContextEntry | None = field(default=None, init=False)
    # Session 内已用的调用 id，与本次响应按 SDK 位置冻结的规范 id（生命周期同 ``_draft_response``）。
    _used_call_ids: set[str] = field(default_factory=set, init=False)
    _response_call_ids: dict[int, str] = field(default_factory=dict, init=False)
    # 回合判定的写入期状态（见 :meth:`_opens_turn`）：Session 已用的最大回合号、本 Run 当前回合（还没写
    # 任何行时为 None）、本 Run 是否已有可见内容（开回合的输入或可见回复）。
    _max_turn: int = field(default=0, init=False)
    _run_turn: int | None = field(default=None, init=False)
    _run_shown: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """恢复 Session 固定系统提示词；其余消息由 :meth:`begin_run` 加载。

        新 Session 使用当前配置构建并持久化完整 system prompt；已有 Session 始终恢复首次持久化的
        内容，因此配置变化不会改写历史 Session 的模型消息前缀。
        """
        store = self.context_store
        self._system_entry = store.get_system_prompt(self.current_session_id)
        if self._system_entry is None:
            system_prompt_text = SystemPromptBuilder.build(
                self.agent, self.workspace.root_path, self.tool_names
            )
            self._system_entry = store.create_system_prompt(
                self.current_session_id,
                {"role": "system", "content": system_prompt_text},
            )

    def begin_run(self, run: RunRecord) -> None:
        """绑定当前 Run，并从数据库重建内存消息列表与序号。

        每次都重建而不是沿用上一个 Run 的内存列表：撤回会把旧行移出上下文，fork、删除
        历史 Run 也会改库，无条件重建让这些用例无需卸载或刷新本对象。代价是每个 Run 多一次读库。
        """

        if run.session_id != self.current_session_id:
            raise ValueError(f"run {run.id} belongs to session {run.session_id}")

        self.current_run_id = run.id
        # 草稿行属于上一个 Run 的调用栈，绑定新 Run 时不得沿用到本次调用。
        self._draft_response = None
        self._response_call_ids = {}
        self._used_call_ids = self.context_store.tool_call_ids(self.current_session_id)
        entries = self.context_store.entries_in_context(self.current_session_id)
        self._entries = [
            entry for entry in entries if entry.sequence != SESSION_SYSTEM_PROMPT_SEQUENCE
        ]
        self._message_sequence = self.context_store.max_sequence(self.current_session_id) + 1
        # 回合号不复用：编辑移出的回合也占号。回合按 Run 而不是按驱动：挂起后唤醒、取消后恢复都是
        # 同一个 Run 的新驱动，从它已写下的行接着判定，不另开回合。
        self._max_turn = self.context_store.max_turn(self.current_session_id)
        written = self.context_store.run_records(self.current_session_id, run.id)
        # 取最大值而不是最后一行：补给本 Run 早先调用的占位排在最后，却属于更早的回合。
        self._run_turn = max((r.turn for r in written), default=None)
        self._run_shown = any(
            _is_turn_input(r.message, r.metadata, r.project_to_transport)
            or _is_visible_reply(r.message)
            for r in written
        )

    @property
    def current_turn(self) -> int | None:
        """本 Run 当前回合号（还没写任何行时为 ``None``）；刚写下的输入或响应就属于这一回合。

        ``prepare_input`` 写完一条回合输入后读它交给消费事件，实时投影据此按回合找到回复。
        """
        return self._run_turn

    def _opens_turn(
        self, message: ChatCompletionMessageParam, metadata: MessageMetadata, project: bool
    ) -> bool:
        """这一行（尚未写入）是否开启新回合：回合规则的唯一实现。

        - Run 写下的第一行开启回合：没有输入开头的回复（唤醒、结果续跑）也是一个回合；
        - 用户输入开启回合；
        - Agent 消息只在本 Run 还没有可见内容（开回合的输入或可见回复）时开回合，否则是当前回复的
          一段（这时这一回合在快照里已有回复，实时投影按回合号把它并进去）；
        - 其余行（回复、通知、工具结果与图片、决定与后台结论）留在当前回合。

        工具结果不经这里，属于发起调用的那一回合。
        """
        if self._run_turn is None:
            return True
        if not _is_turn_input(message, metadata, project):
            return False
        return cast(UserMessageMetadata, metadata).source is None or not self._run_shown

    def _turn_for_new_row(
        self, message: ChatCompletionMessageParam, metadata: MessageMetadata, project: bool
    ) -> int:
        """按 :meth:`_opens_turn` 取这一行的回合号并推进写入期状态；调用方随即写库。"""
        if self._opens_turn(message, metadata, project):
            self._max_turn += 1
            self._run_turn = self._max_turn
        if _is_turn_input(message, metadata, project) or _is_visible_reply(message):
            self._run_shown = True
        return cast(int, self._run_turn)

    def _track(self, entry: ContextEntry, *, in_context: bool) -> None:
        """内存消息列表与序号的唯一推进入口；调用方保证该行已落库。

        序号按已落库行推进（流式草稿落库即占号，提交时不再推进）；只有进入模型上下文的行才加入
        内存列表。
        """
        self._message_sequence = max(self._message_sequence, entry.sequence + 1)
        if in_context:
            self._entries.append(entry)

    def add_message(
        self,
        message: ChatCompletionMessageParam,
        *,
        include_in_context: bool = True,
        metadata: MessageMetadata | None = None,
        project_to_transport: bool = True,
        message_id: int | None = None,
        db: DbSession | None = None,
    ) -> Literal["appended", "replaced"]:
        """写入完整 SDK 消息；工具结果必须属于当前最后一批调用的同一 Run。

        外部消息队列投递来的用户输入与通知也经本方法写入，不另设接纳入口。
        ``message_id`` 只有用户输入需要传：沿用队列行身份，排队与已消费阶段前端消息 id 不变；
        其余消息没有身份。``db`` 让追加加入调用方事务（``prepare_input`` 把队列确认与写入放在
        同一事务）；内存列表随即推进、不等提交——事务失败会经 Run 失败边界结束本次驱动，下个 Run
        的 :meth:`begin_run` 从库重建，不会留下错位的 working copy。

        当前批次里已有的结果（如取消、中断占位）原地覆盖，保留消息身份和序号。HITL 的等待
        占位不走这里：决定可能在之后的模型步甚至之后的 Run 才到，届时调用已不属于当前批次，
        所以执行结果统一另行追加为 user 消息（见 ``settle_decision_observation``），占位只经
        :meth:`update_tool_result_metadata` 更新卡片状态，历史前缀因此不变。缺少调用依据的新
        结果在写库前报 ContextProtocolError，不伪造调用或丢弃执行结果。流式预览的取消占位
        可以没有纳入上下文的调用，但必须 include_in_context=False，仅供 Transport 重建。
        替换时提交完整 metadata，旧占位状态和错误展示不会被隐式保留。
        """

        target_run_id = self.current_run_id
        # 类型窄化不进入推导式作用域，故先固化标识；后续日志与覆盖分支都复用它。
        tool_call_id = message.get("tool_call_id") if message["role"] == "tool" else ""
        existing: ContextEntry | None = None
        if message["role"] == "tool":
            plans = self._plan_tool_calls()
            plan = plans[-1] if plans else None
            paired = (
                next((slot for slot in plan.slots if slot.call_id == tool_call_id), None)
                if plan is not None and plan.target_run_id == target_run_id
                else None
            )
            if paired is None and include_in_context:
                error = ContextProtocolError(
                    "tool_result_without_current_call",
                    # 只用于定位错误：这行没有写入，回合号取当前值。
                    ContextEntry(
                        message, target_run_id, self._message_sequence, turn=self._max_turn
                    ),
                    message["tool_call_id"],
                )
                self._log_protocol_error(error)
                raise error
            if paired is not None and paired.tool_index is not None:
                existing = self._entries[paired.tool_index]
            owner = self._entries[plan.target_index] if paired is not None and plan else None
        resolved_metadata = (
            metadata if metadata is not None else empty_message_metadata(message["role"])
        )
        if existing is not None:
            # 同一批次内，调用可能先写了取消或中断占位，随后才拿到真实结果；
            # 用 replace_message(...) 保留原来的 sequence 和 run_id、更新消息内容与 Transport
            # 元数据，库里始终只有一条结果，不会同一调用留两行。
            self.context_store.replace_message(
                SessionContextRecord(
                    session_id=self.current_session_id,
                    run_id=existing.run_id,
                    message_id=existing.message_id,
                    message=message,
                    include_in_context=True,
                    sequence=existing.sequence,
                    turn=existing.turn,
                    project_to_transport=project_to_transport,
                    metadata=resolved_metadata,
                )
            )
            existing.message = message
            existing.metadata = resolved_metadata
            log.info(
                "tool_result_row_replaced",
                extra={
                    "msg": "同一工具调用的既有结果行已被真实结果覆盖",
                    "data": {
                        "session_id": self.current_session_id,
                        "run_id": existing.run_id,
                        "tool_call_id": tool_call_id,
                        "sequence": existing.sequence,
                    },
                },
            )
            return "replaced"
        if message["role"] == "tool":
            # 已配对的结果属于发起调用的回合；流式取消预览（未配对、不进上下文）留在当前回合。
            turn = (
                owner.turn
                if owner is not None
                else self._turn_for_new_row(message, resolved_metadata, project_to_transport)
            )
        else:
            turn = self._turn_for_new_row(message, resolved_metadata, project_to_transport)
        self._append_message(
            message,
            target_run_id,
            include_in_context,
            resolved_metadata,
            project_to_transport,
            turn=turn,
            message_id=message_id,
            db=db,
        )
        return "appended"

    def _append_message(
        self,
        message: ChatCompletionMessageParam,
        run_id: int | None,
        include_in_context: bool,
        metadata: MessageMetadata,
        project_to_transport: bool = True,
        *,
        turn: int,
        message_id: int | None = None,
        db: DbSession | None = None,
    ) -> ContextEntry:
        """调用方已验证结果归属并取好回合号；写库成功后才推进内存列表和序号。"""
        entry = self.context_store.append(
            self.current_session_id,
            run_id,
            message,
            self._message_sequence,
            include_in_context,
            metadata,
            project_to_transport=project_to_transport,
            db=db,
            turn=turn,
            message_id=message_id,
        )
        self._track(entry, in_context=include_in_context)
        return entry

    def latest_notice(self, notice_kind: str) -> tuple[int | None, NoticeEntry] | None:
        """上下文里最近一条 ``notice_kind`` 通知与它所在行的 Run（继承行为 ``None``）。

        驱动开始时推导 Run 级通知的比对基准（``drive_notices``）：基准取自上下文而不是进程内记忆，
        重启、缓存卸载、撤回与分叉之后都与模型实际看到的一致。
        """
        for entry in reversed(self._entries):
            metadata = entry.metadata
            if isinstance(metadata, UserMessageMetadata) and metadata.kind == "notice":
                for notice in reversed(metadata.notices):
                    if notice.notice_kind == notice_kind:
                        return entry.run_id, notice
        return None

    def find_tool_call(
        self, tool_call_id: str
    ) -> ChatCompletionMessageFunctionToolCallParam | None:
        """在纳入上下文的 assistant 消息里按身份找回一次工具调用（含名称与原始参数）。

        跨 Run 查找：HITL 调用可能在早先的 Run 里发起，决定在之后的 Run 才到。找不到（如已被
        撤回移出上下文）时返回 ``None``，由调用方决定如何告知模型。
        """
        for entry in reversed(self._entries):
            if entry.run_id is None or entry.message["role"] != "assistant":
                continue
            for call in entry.message.get("tool_calls") or ():
                if call["id"] == tool_call_id and call["type"] == "function":
                    return call
        return None

    def tool_result_metadata(self, tool_call_id: str) -> ToolMessageMetadata | None:
        """按调用身份取工具结果行的展示 metadata（跨 Run）；没有结果行时返回 ``None``。"""
        for entry in reversed(self._entries):
            if (
                entry.run_id is not None
                and entry.message["role"] == "tool"
                and entry.message["tool_call_id"] == tool_call_id
            ):
                metadata = entry.metadata
                return metadata if isinstance(metadata, ToolMessageMetadata) else None
        return None

    def update_tool_result_metadata(
        self, tool_call_id: str, metadata: ToolMessageMetadata, *, db: DbSession | None = None
    ) -> ToolMessageMetadata:
        """只替换一条工具结果行的展示 metadata，模型可见正文不变；返回实际写入的 metadata。

        HITL 执行结果另行追加为消息，等待占位仍是模型协议上的结果；这里只把占位的卡片状态推到
        终态，历史前缀因此不变。结果没有带
        展示数据时沿用占位的（HITL 卡片由占位的展示数据决定，如交接的页面预览）。传入
        ``db`` 时加入调用方事务（接收决定时与确认同一事务把占位推到 ``running``）。

        异常:
            KeyError: 上下文中没有该调用的结果行。
        """
        entry = next(
            (
                item
                for item in reversed(self._entries)
                if item.run_id is not None
                and item.message["role"] == "tool"
                and item.message["tool_call_id"] == tool_call_id
            ),
            None,
        )
        if entry is None:
            raise KeyError(tool_call_id)
        if isinstance(entry.metadata, ToolMessageMetadata):
            previous = entry.metadata
            # 显式构造而不用 ``model_copy(update=...)``：后者不校验键名，拼错的键会被静默丢弃。
            metadata = ToolMessageMetadata(
                status=metadata.status,
                error=metadata.error,
                display_data=metadata.display_data or previous.display_data,
                background=metadata.background or previous.background,
            )
        self.context_store.replace_message(
            SessionContextRecord(
                session_id=self.current_session_id,
                run_id=entry.run_id,
                message_id=entry.message_id,
                message=entry.message,
                include_in_context=True,
                sequence=entry.sequence,
                turn=entry.turn,
                project_to_transport=True,
                metadata=metadata,
            ),
            db=db,
        )
        entry.metadata = metadata
        return metadata

    def save_model_response(
        self,
        snapshot: ParsedChatCompletionSnapshot,
        *,
        include_in_context: bool,
        registered_tool_names: Collection[str],
    ) -> ContextEntry:
        """直接保存 SDK 快照；同一次调用内持续替换同一行，不重新聚合 chunk。

        行身份由本层持有，调用方不再传递。流式写入（``include_in_context=False``）只保留身份
        供下一帧替换，不进入 working copy；响应结束并完成工具名过滤后，仅一次以
        ``include_in_context=True`` 提交，提交即清空草稿，下一次调用自然新起一行。中断行继续
        留库供展示，后续模型上下文不会加载它——它所在的模型调用要么已提交、要么终结了 Run，
        因此不会出现「下一次调用沿用中断行身份」的情况。
        """
        choice = snapshot.choices[0]
        self._freeze_tool_call_ids(choice.message.tool_calls or [])
        message = assistant_message(choice.message, registered_tool_names)
        metadata = AssistantMessageMetadata(
            completion_id=snapshot.id,
            model=snapshot.model,
            finish_reason=choice.finish_reason,
            usage=snapshot.usage,
        )
        draft = self._draft_response
        if draft is None:
            entry = self._append_message(
                message,
                self.current_run_id,
                include_in_context,
                metadata,
                turn=self._turn_for_new_row(message, metadata, True),
            )
            # ``_append_message`` 已按准入决定写 working copy；只有未提交时需留身份供下一帧替换。
            if include_in_context:
                self._response_call_ids = {}
            else:
                self._draft_response = entry
            return entry
        if _is_visible_reply(message):
            # 草稿首帧常是空的，正文与调用在后续帧才到；回合判定要知道本 Run 已有可见回复。
            self._run_shown = True
        self.context_store.replace_message(
            SessionContextRecord(
                session_id=self.current_session_id,
                run_id=draft.run_id,
                message_id=draft.message_id,
                message=message,
                include_in_context=include_in_context,
                sequence=draft.sequence,
                turn=draft.turn,
                metadata=metadata,
            )
        )
        draft.message = message
        draft.metadata = metadata
        if include_in_context:
            # 提交进模型上下文即本次调用结束：清空草稿身份，下一次调用新起一行。
            self._track(draft, in_context=True)
            self._draft_response = None
            self._response_call_ids = {}
        return draft

    def _freeze_tool_call_ids(self, calls: list[ParsedFunctionToolCall]) -> None:
        """就地把快照里的调用 id 改成 Session 内唯一的规范 id，全部下游都从这里读到同一份身份。

        决定、取消、占位结算、看板和前端开合都按 id 寻址，而厂商可能跨响应甚至在同一响应里重复
        id。只在冲突时改写：表现正常的厂商字节不变（Kimi K2 等依赖自己的 id 形状，且会模仿历史
        里的 id）。改写发生在首次回传模型之前，之后历史不再变化，不破坏前缀缓存。后缀只用
        ``_`` 与数字：部分网关的 id 只接受 ``[A-Za-z0-9_-]``，改写出非法 id 后每次请求都会被拒。

        身份按 SDK 位置冻结：id 首次出现后不再跟随后续分片，顺带挡住每片重发 id 的厂商（SDK
        会把它拼成 ``call_0call_0``）。
        """
        for position, call in enumerate(calls):
            # SDK 草稿用 model_construct 构造，字段可能尚未齐全；这是第三方输入边界。
            raw = getattr(call, "id", None)
            if not isinstance(raw, str) or not raw.strip():
                continue
            canonical = self._response_call_ids.get(position)
            if canonical is None:
                canonical, suffix = raw, 1
                while canonical in self._used_call_ids:
                    suffix += 1
                    canonical = f"{raw}_{suffix}"
                self._used_call_ids.add(canonical)
                self._response_call_ids[position] = canonical
            call.id = canonical

    def abandon_model_response(self) -> None:
        """结束未准入的响应草稿；保留数据库展示行，下一次模型请求使用新消息身份。"""
        self._draft_response = None
        self._response_call_ids = {}

    def _log_protocol_error(self, error: ContextProtocolError) -> None:
        """记录可定位的上下文完整性错误，不泄露工具正文到 UI 错误通道。"""
        log.error(
            "runtime_context_tool_protocol_invalid",
            extra={
                "msg": "上下文工具结果缺少可靠配对依据，保留事实并停止执行",
                "data": {
                    "session_id": self.current_session_id,
                    "run_id": self.current_run_id,
                    **error.log_data(),
                },
            },
        )

    def _plan_tool_calls(self) -> tuple[ToolCallClosurePlan, ...]:
        try:
            return plan_tool_call_closures(self._entries)
        except ContextProtocolError as error:
            self._log_protocol_error(error)
            raise

    def close_unclosed_tool_calls(self) -> None:
        """按全部批次恢复模型协议顺序；只为真实调用补中断占位，不重放工具。

        先完成整段配对校验，避免遇到孤儿时已经写入部分占位。已落库结果紧跟所属调用，
        其余消息保持相对顺序；数据库序号不变，因此每次冷加载都按同一规则投影全部历史。
        占位沿用调用的 Run，持久化后可重建；协议错误保留事实并向 Run 失败边界传播。
        """
        plans = self._plan_tool_calls()
        if not plans:
            return
        entries = list(self._entries)
        by_index = {plan.target_index: plan for plan in plans}
        claimed = {index for plan in plans for index in plan.claimed_tool_indices}
        normalized: list[ContextEntry] = []
        created = 0
        reordered = 0
        for index, entry in enumerate(entries):
            if index in claimed:
                continue
            normalized.append(entry)
            plan = by_index.get(index)
            if plan is None:
                continue
            for offset, slot in enumerate(plan.slots, start=1):
                if slot.tool_index is not None:
                    normalized.append(entries[slot.tool_index])
                    reordered += int(slot.tool_index != index + offset)
                else:
                    normalized.append(
                        self._append_message(
                            build_placeholder_tool_message(slot.call_id, slot.tool_name),
                            plan.target_run_id,
                            True,
                            ToolMessageMetadata(status="cancelled"),
                            # 补的占位属于发起调用的那一回合。
                            turn=entry.turn,
                        )
                    )
                    created += 1
        self._entries = normalized
        if created or reordered:
            log.warning(
                "runtime_context_tool_call_protocol_repaired",
                extra={
                    "msg": "上下文中的工具调用结果已闭合或恢复到合法消息顺序",
                    "data": {
                        "session_id": self.current_session_id,
                        "created_placeholder_count": created,
                        "reordered_tool_count": reordered,
                    },
                },
            )

    def load_message(self) -> list[ChatCompletionMessageParam]:
        """闭合工具协议后，由内存消息列表返回**模型请求副本**：附件定位符已解析为 data URL。

        canonical context 只保存 ``cosir-attachment://`` 定位符，解析结果（base64）只存在于返回
        副本，不会回写 working copy。返回副本由 ``resolve_messages_for_model`` 统一深拷贝。

        副作用:
            命中图片附件时读文件并 base64 编码，属于阻塞 IO；调用方必须放到工作线程执行
            （``asyncio.to_thread``），不得在事件循环里直接调用。
        """
        self.close_unclosed_tool_calls()
        return resolve_messages_for_model(
            [entry.message for entry in self._effective_entries()],
            workspace_id=self.workspace.id,
        )

    def _effective_entries(self) -> list[ContextEntry]:
        """返回 system、历史和当前 run 条目的有序列表。"""

        if self._system_entry is None:
            raise RuntimeError("system entry is not initialized")
        return [self._system_entry, *self._entries]
