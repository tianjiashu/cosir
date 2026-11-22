"""分叉的来源：按回合切分一个 Session 的上下文，把切点之前的内容复制成新 Session 的历史，不创建历史 Run。

手动分叉切在所选回复这一回合的结尾，Agent 分叉切在调用所在回合的开头。回合是每行写入时记下的
事实（``turn``，规则只在 ``RuntimeMessageContextManager._opens_turn``），这里只按回合号查找，不从
``run_id`` / ``project_to_transport`` 推导——复制会改写这两项，推导出的边界因此会在分支里漂移。
副本原样保留回合号，分支与来源的回合划分一致；副本没有本地 Run，Agent 分叉靠
``project_to_transport`` 隐藏它们，手动分叉恢复对话角色的展示，内部 user 种类的消息保持隐藏。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import cast

from openai.types.chat import (
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
)
from sqlalchemy.orm import Session as DbSession

from app.core.context.context_entry import ContextEntry
from app.core.context.context_store import ContextStore
from app.core.context.tool_call_closure import plan_tool_call_closures
from app.core.workflow.message_queue.message_bodies.notice import NoticeBody
from app.models.message_context.message_metadata.base.message_kinds import TURN_INPUT_KINDS
from app.models.message_context.message_metadata.tool_message_metadata import ToolMessageMetadata
from app.models.message_context.message_metadata.user_message_metadata import UserMessageMetadata
from app.models.message_context.session_context import SessionContextRecord, turn_withdrawn
from app.models.run.run_record import RunRecord
from app.models.run.run_status import ACTIVE_RUN_STATUSES
from app.models.session.session_extra import SessionFork, SessionForkLaunch
from app.models.session.session_fork_errors import SessionForkConflictError
from app.models.session.session_kind import SessionType
from app.models.session.session_record import SessionRecord
from app.storage.crud.session_context_crud import SessionContextCrud
from app.utils.path.system_cosir import session_scratch_dir

_NOT_INHERITED_CONTENT = (
    "The result of this tool call was not inherited in this branch. No tool output is available."
)
_NOT_INHERITED_HINT = "此分叉未继承调用结果"
# 分支的身份声明（``ForkSource.identity_notice``）。Agent 分叉的交接通知：继承的历史是第一人称的，分叉自己
# 分不清哪些事归它——曾把来源没做完的请求接过去，并自认是父会话；所以任务说明前先写清身份与会话归属。
_AGENT_FORK_HANDOVER = (
    "You are session {fork_id}, a fork of session {source_id}. The conversation above was copied from "
    "that session as background: requests in it belong to that session, even where they read as your "
    "own work, so do not pick them up. Your only job is the assignment in the next message. Each "
    "time you finish working on it, your final reply is reported to session {source_id} "
    "automatically, so do not send it with session_send. If the user later talks to you here, "
    "follow the user; what you do for the user is not reported."
)
# 分叉沿用来源的系统提示词（接缓存），可写根只能在交接通知里告知；来源自己也收窄过时以这一条为准。
_WRITABLE_ROOT = (
    " Your writable root is {writable_root} (relative to the workspace root): file-writing tools "
    "are refused outside it and commands start there by default. This replaces any writable "
    "area stated earlier. Do not change files outside it by other means; report what needs to "
    "change there instead."
)
# 手动分叉的身份声明，每次都发：系统提示词是来源的（接缓存），里面的临时目录属于来源；复制来的历史里还可能有
# 一条交接通知（「你是会话 B，结果汇报给 A」），换了 Session 就不成立，而上下文只追加、不改写历史行。措辞
# 写成「如果有，就不再适用」，复制到没复制到交接通知都成立，再手动分叉一次也成立。
_MANUAL_FORK_IDENTITY = (
    "You are session {target_id}, a copy the user made of session {source_id}. Any earlier notice "
    "above that names a different session as you or says your replies are reported to another session "
    "no longer applies. You work for the user here, and nothing you do is reported automatically."
)
# 临时目录按会话 id 推导，分叉沿用来源的系统提示词，里面写的是来源的目录（文件工具写不进去）；两种分叉都
# 在身份声明里更正，与可写根同一种写法。
_SCRATCH_DIR = (
    " Your scratch directory is {scratch_dir}; it replaces any scratch directory stated earlier."
)


@dataclass(frozen=True)
class ForkCut:
    """分叉的切点：复制序号小于 ``before_sequence`` 的行，分支以 ``launch`` 作为初始配置。"""

    before_sequence: int
    launch: SessionForkLaunch


@dataclass(frozen=True)
class ForkSource:
    """一个来源 Session 在分叉写事务里读到的全部行与 Run；切点、初始配置与复制都按它算。

    ``rows`` 是全部 context 行（含不进上下文、不投影的）：切点与复制都要看到撤回移出与隐藏的行。
    ``answered_in_queue`` 是来源外部队列里还没并入的消息送达了结局的调用（``answered_calls``）：结局
    还在队列里的调用同样不在复制范围内。
    """

    session: SessionRecord
    rows: list[SessionContextRecord]
    runs: list[RunRecord]
    answered_in_queue: frozenset[str] = frozenset()

    def cut_after_turn(self, turn: int) -> ForkCut:
        """手动分叉：切在所选回复这一回合的结尾，配置取这一回合所在的 Run。

        异常:
            KeyError: 回合不在这个 Session 的对话里或已被撤回，映射 404。
            SessionForkConflictError: 这是最后一个回合且 Session 还有活动 Run（``FORK_REPLY_ACTIVE``）。
        """
        start = self.turn_start(turn)
        before_sequence = next(
            (row.sequence for row in self._conversation() if row.turn > start.turn), None
        )
        if before_sequence is None:
            if any(run.status in ACTIVE_RUN_STATUSES for run in self.runs):
                raise SessionForkConflictError("FORK_REPLY_ACTIVE", "this reply is still running")
            before_sequence = self.rows[-1].sequence + 1
        return ForkCut(before_sequence, self._launch_of(start.run_id))

    def cut_before_call(self, run_id: int, call_id: str) -> ForkCut:
        """Agent 分叉：切在调用所在回合的开头，用户这一轮的话和这一轮的过程都不进分支。

        异常:
            KeyError: 这个 Run 没有发起过这次调用。
        """
        conversation = self._conversation()
        call_row = next(
            (
                r
                for r in reversed(conversation)
                if r.run_id == run_id
                and r.include_in_context
                and r.message["role"] == "assistant"
                and any(
                    c["id"] == call_id
                    for c in cast(
                        list[ChatCompletionMessageFunctionToolCallParam],
                        r.message.get("tool_calls", []),
                    )
                )
            ),
            None,
        )
        if call_row is None:
            raise KeyError(call_id)
        before_sequence = next(r.sequence for r in conversation if r.turn == call_row.turn)
        return ForkCut(before_sequence, self._launch_of(run_id))

    def turn_start(self, turn: int) -> SessionContextRecord:
        """返回回合 ``turn`` 的首行。

        回合已被撤回时同样拒绝：撤回会留下「更早调用在这一回合才并入的结果」，它们仍带这个回合号、
        仍是保留行，只看保留行就会切在一个已撤回的回合上。

        异常:
            KeyError: 回合不在这个 Session 的对话里或已被撤回。
        """
        turn_rows = [r for r in self.rows if r.turn == turn and r.sequence > 0]
        if not turn_rows or turn_withdrawn(turn_rows):
            raise KeyError(turn)
        return min(turn_rows, key=lambda r: r.sequence)

    def copy_before(
        self,
        cut: ForkCut,
        target: SessionRecord,
        crud: SessionContextCrud,
        db: DbSession,
    ) -> int:
        """把切点之前的保留行复制给 ``target``，切点两侧被切开的调用在这里收尾；返回复制的行数。

        缺结果的调用补一条未继承结果，配对先按原 Run 身份做，后面同 id 的结果才不会顶替前一个 Run
        缺失的那条；孤立或重复的结果中止调用方的事务。继承行收不到执行更新，``run_id`` 为空已表达这
        一点。不复制来源 Run 的状态、用量与耗时。
        """
        before_sequence = cut.before_sequence
        prefix = [r for r in self.rows if r.retained and r.sequence < before_sequence]
        system = next((r for r in prefix if r.sequence == 0), None)
        if system is None:
            raise RuntimeError(f"session {self.session.id} has no persisted system prompt")
        ContextStore.to_system_prompt_entry(self.session.id, system)  # 只做校验：系统提示词须合法
        entries = [
            ContextEntry(r.message, r.run_id, r.sequence, r.message_id, r.metadata, turn=r.turn)
            for r in prefix
            if r.include_in_context
        ]
        repairs: dict[int, list[SessionContextRecord]] = {}
        for plan in plan_tool_call_closures(entries):
            owner = entries[plan.target_index]
            repairs[owner.sequence] = [
                SessionContextRecord(
                    session_id=target.id,
                    run_id=None,
                    sequence=0,
                    turn=owner.turn,
                    include_in_context=True,
                    project_to_transport=False,
                    message={
                        "role": "tool",
                        "tool_call_id": slot.call_id,
                        "content": _NOT_INHERITED_CONTENT,
                    },
                    metadata=_not_inherited_metadata(),
                )
                for slot in plan.missing_slots
            ]
        unresolved = self._unresolved_calls(before_sequence)
        hide = target.session_type == SessionType.AGENT_FORK
        restore = self.session.session_type == SessionType.AGENT_FORK
        result: list[SessionContextRecord] = []
        for row in prefix:
            project = row.project_to_transport
            if hide:
                project = False
            elif restore and row.run_id is None:
                project = _visible_in_manual_fork(row)
            settled = row.message["role"] == "tool" and row.message["tool_call_id"] in unresolved
            message: ChatCompletionMessageParam = deepcopy(row.message)
            if message["role"] == "tool" and settled:
                # tool 消息只有这三个键：结论没继承过来，正文换成未继承说明。
                message = {
                    "role": "tool",
                    "tool_call_id": message["tool_call_id"],
                    "content": _NOT_INHERITED_CONTENT,
                }
            result.append(
                replace(
                    row,
                    id=None,
                    session_id=target.id,
                    run_id=None,
                    message=message,
                    metadata=_not_inherited_metadata() if settled else deepcopy(row.metadata),
                    project_to_transport=project,
                )
            )
            result.extend(repairs.get(row.sequence, []))
        for sequence, row in enumerate(result):
            crud.create(replace(row, sequence=sequence), db=db)
        return len(result)

    def _conversation(self) -> list[SessionContextRecord]:
        """切分只看保留下来的对话行：系统提示词不属于回合，撤回移出的行不算。"""
        return [r for r in self.rows if r.retained and r.sequence > 0]

    def _launch_of(self, run_id: int | None) -> SessionForkLaunch:
        """本地回合取它所在 Run 的发起配置，继承来的回合取来源自己的初始配置。"""
        if run_id is None:
            return cast(SessionFork, self.session.extra.fork).initial_launch.model_copy(deep=True)
        return SessionForkLaunch.from_run(next(r for r in self.runs if r.id == run_id))

    def _unresolved_calls(self, before_sequence: int) -> frozenset[str]:
        """占位在切点前、结局不在复制范围内的调用。

        占位正文只是「已提交」「等待决定」：还在等的占位没有结局，已结算的占位结局是一条按调用寻址的
        消息（后台结论、决定，以及原地结算时的作废 / 重启通知，见 ``NoticeBody.tool_call_ids``）。结局
        在切点之后的行里、或还在来源队列里没并入，分支都收不到；原样继承的话，分支里的卡片永远转圈，
        模型也以为工作还在进行（原地结算的占位卡片已是「已取消」，模型却仍读到「等待决定」）。
        """
        answered_outside = set(self.answered_in_queue) | {
            call
            for r in self.rows
            if r.sequence >= before_sequence and isinstance(r.metadata, UserMessageMetadata)
            for call in r.metadata.answered_calls
        }
        waiting = {
            r.message["tool_call_id"]
            for r in self.rows
            if r.sequence < before_sequence
            and r.message["role"] == "tool"
            and isinstance(r.metadata, ToolMessageMetadata)
            and r.metadata.status in {"pending", "running"}
        }
        return frozenset(answered_outside | waiting)

    def identity_notice(self, target: SessionRecord) -> NoticeBody | None:
        """分支的身份声明，由调用方在建分支的同一写事务里入队；不是分叉时为 ``None``。

        「分支是谁、和来源什么关系」是分叉自己的事实（会话类型、派生调用、可写根都在建分支时定下），所以
        声明归这里，不归发起分叉的工具；与分支同一事务写入，分支存在就一定带着它，不会留下不知道自己是谁的
        分支。分叉原样沿用来源的系统提示词，所以凡是按会话不同的事实（会话 id、临时目录、可写根）都只能在
        这里更正。Agent 分叉：交接通知（收窄了可写根时附上范围）；手动分叉：身份更正。两种都写明自己的
        临时目录。
        """
        scratch = _SCRATCH_DIR.format(scratch_dir=session_scratch_dir(target.id))
        if target.session_type == SessionType.AGENT_FORK:
            content = _AGENT_FORK_HANDOVER.format(fork_id=target.id, source_id=self.session.id)
            if target.extra.writable_root is not None:
                content += _WRITABLE_ROOT.format(writable_root=target.extra.writable_root)
            return NoticeBody(content=content + scratch, notice_kind="fork_handover")
        if target.session_type != SessionType.USER_FORK:
            return None
        return NoticeBody(
            content=_MANUAL_FORK_IDENTITY.format(target_id=target.id, source_id=self.session.id)
            + scratch,
            notice_kind="fork_handover",
        )


def _visible_in_manual_fork(row: SessionContextRecord) -> bool:
    """手动分叉恢复对话内容的展示；内部 user 种类（通知、决定、工具结果与图片）永远不成为气泡。

    Agent 分叉额外隐藏了普通输入与 assistant / tool 行；按行已有的角色与种类就能判断手动分支该显示
    什么，不必另存一个可见性标记。
    """
    if row.message["role"] == "user":
        return (
            isinstance(row.metadata, UserMessageMetadata) and row.metadata.kind in TURN_INPUT_KINDS
        )
    return row.message["role"] in {"assistant", "tool"}


def _not_inherited_metadata() -> ToolMessageMetadata:
    return ToolMessageMetadata(status="cancelled", error=_NOT_INHERITED_HINT)
