"""分叉的来源：按回合切分一个 Task 的上下文，把切点之前的内容复制成新 Task 的历史，不创建历史 Run。

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
from sqlalchemy.orm import Session

from app.core.context.context_entry import ContextEntry
from app.core.context.context_store import ContextStore
from app.core.context.tool_call_closure import plan_tool_call_closures
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord, turn_withdrawn
from app.models.enums.conversation_run_status import ACTIVE_RUN_STATUSES
from app.models.errors.task_fork_errors import TaskForkConflictError
from app.models.message_metadata import TURN_INPUT_KINDS, ToolMessageMetadata, UserMessageMetadata
from app.models.task_extra import TaskFork, TaskForkLaunch
from app.models.task_kind import TaskType
from app.models.task_record import TaskRecord
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud

_NOT_INHERITED_CONTENT = (
    "The result of this tool call was not inherited in this branch. No tool output is available."
)
_NOT_INHERITED_HINT = "此分叉未继承调用结果"


@dataclass(frozen=True)
class ForkCut:
    """分叉的切点：复制序号小于 ``before_sequence`` 的行，分支以 ``launch`` 作为初始配置。"""

    before_sequence: int
    launch: TaskForkLaunch


@dataclass(frozen=True)
class ForkSource:
    """一个来源 Task 在分叉写事务里读到的全部行与 Run；切点、初始配置与复制都按它算。

    ``rows`` 是全部 context 行（含不进上下文、不投影的）：切点与复制都要看到撤回移出与隐藏的行。
    """

    task: TaskRecord
    rows: list[ConversationTaskContextRecord]
    runs: list[ConversationRunRecord]

    def cut_after_turn(self, turn: int) -> ForkCut:
        """手动分叉：切在所选回复这一回合的结尾，配置取这一回合所在的 Run。

        异常:
            KeyError: 回合不在这个 Task 的对话里或已被撤回，映射 404。
            TaskForkConflictError: 这是最后一个回合且 Task 还有活动 Run（``FORK_REPLY_ACTIVE``）。
        """
        start = self.turn_start(turn)
        before_sequence = next(
            (row.sequence for row in self._conversation() if row.turn > start.turn), None
        )
        if before_sequence is None:
            if any(run.status in ACTIVE_RUN_STATUSES for run in self.runs):
                raise TaskForkConflictError("FORK_REPLY_ACTIVE", "this reply is still running")
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

    def turn_start(self, turn: int) -> ConversationTaskContextRecord:
        """返回回合 ``turn`` 的首行。

        回合已被撤回时同样拒绝：撤回会留下「更早调用在这一回合才并入的结果」，它们仍带这个回合号、
        仍是保留行，只看保留行就会切在一个已撤回的回合上。

        异常:
            KeyError: 回合不在这个 Task 的对话里或已被撤回。
        """
        turn_rows = [r for r in self.rows if r.turn == turn and r.sequence > 0]
        if not turn_rows or turn_withdrawn(turn_rows):
            raise KeyError(turn)
        return min(turn_rows, key=lambda r: r.sequence)

    def copy_before(
        self,
        cut: ForkCut,
        target: TaskRecord,
        crud: ConversationTaskContextCrud,
        session: Session,
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
            raise RuntimeError(f"task {self.task.id} has no persisted system prompt")
        ContextStore.to_system_prompt_entry(self.task.id, system)  # 只做校验：系统提示词须合法
        entries = [
            ContextEntry(r.message, r.run_id, r.sequence, r.message_id, r.metadata, turn=r.turn)
            for r in prefix
            if r.include_in_context
        ]
        repairs: dict[int, list[ConversationTaskContextRecord]] = {}
        for plan in plan_tool_call_closures(entries):
            owner = entries[plan.target_index]
            repairs[owner.sequence] = [
                ConversationTaskContextRecord(
                    task_id=target.id,
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
        hide = target.task_type == TaskType.AGENT_FORK
        restore = self.task.task_type == TaskType.AGENT_FORK
        result: list[ConversationTaskContextRecord] = []
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
                    task_id=target.id,
                    run_id=None,
                    message=message,
                    metadata=_not_inherited_metadata() if settled else deepcopy(row.metadata),
                    project_to_transport=project,
                )
            )
            result.extend(repairs.get(row.sequence, []))
        for sequence, row in enumerate(result):
            crud.create(replace(row, sequence=sequence), session=session)
        return len(result)

    def _conversation(self) -> list[ConversationTaskContextRecord]:
        """切分只看保留下来的对话行：系统提示词不属于回合，撤回移出的行不算。"""
        return [r for r in self.rows if r.retained and r.sequence > 0]

    def _launch_of(self, run_id: int | None) -> TaskForkLaunch:
        """本地回合取它所在 Run 的发起配置，继承来的回合取来源自己的初始配置。"""
        if run_id is None:
            return cast(TaskFork, self.task.extra.fork).initial_launch.model_copy(deep=True)
        return TaskForkLaunch.from_run(next(r for r in self.runs if r.id == run_id))

    def _unresolved_calls(self, before_sequence: int) -> frozenset[str]:
        """占位在切点前、结论不在切点前的调用。

        占位正文只是「已提交」「等待决定」：还在等的占位没有结论，已结算的占位结论写在后面的结果消息
        里（占位只更新展示状态）。这两种原样继承，分支里的卡片永远转圈，模型也以为工作还在进行。
        """
        answered_later = {
            r.metadata.tool_call_id
            for r in self.rows
            if r.sequence >= before_sequence
            and isinstance(r.metadata, UserMessageMetadata)
            and r.metadata.tool_call_id is not None
        }
        waiting = {
            r.message["tool_call_id"]
            for r in self.rows
            if r.sequence < before_sequence
            and r.message["role"] == "tool"
            and isinstance(r.metadata, ToolMessageMetadata)
            and r.metadata.status in {"pending", "running"}
        }
        return frozenset(answered_later | waiting)


def _visible_in_manual_fork(row: ConversationTaskContextRecord) -> bool:
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
