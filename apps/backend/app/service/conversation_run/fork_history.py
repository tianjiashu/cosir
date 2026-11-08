"""按回合边界切分 Task 上下文，把切点之前的内容复制成新 Task 的历史，不创建历史 Run。

手动分叉切在所选回复这一回合的结尾，Agent 分叉切在调用所在回合的开头，两者是同一条回合规则。
副本没有本地 Run；Agent 分叉靠 ``project_to_transport`` 隐藏它们，
手动分叉恢复对话角色的展示，内部 user 种类的消息保持隐藏。
"""

from copy import deepcopy
from dataclasses import replace
from typing import cast

from openai.types.chat import ChatCompletionMessageFunctionToolCallParam
from sqlalchemy.orm import Session

from app.core.context.context_entry import ContextEntry
from app.core.context.context_store import ContextStore
from app.core.context.tool_call_closure import plan_tool_call_closures
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.fork_reply import ForkReply
from app.models.message_metadata import TURN_INPUT_KINDS, ToolMessageMetadata, UserMessageMetadata
from app.models.task_extra import TaskFork, TaskForkLaunch
from app.models.task_kind import TaskType
from app.models.task_record import TaskRecord
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud

_NOT_INHERITED_CONTENT = (
    "The result of this tool call was not inherited in this branch. No tool output is available."
)
_NOT_INHERITED_HINT = "此分叉未继承调用结果"


def _is_input(row: ConversationTaskContextRecord) -> bool:
    return (
        isinstance(row.metadata, UserMessageMetadata)
        and row.metadata.kind in TURN_INPUT_KINDS
        and row.project_to_transport
    )


def _conversation(
    rows: list[ConversationTaskContextRecord],
) -> list[ConversationTaskContextRecord]:
    """切分只看保留下来的对话行：系统提示词不属于回合，编辑移出的行不再存在。"""
    return [r for r in rows if r.retained and r.sequence > 0]


def _starts_turn(
    previous: ConversationTaskContextRecord, row: ConversationTaskContextRecord
) -> bool:
    """回合从一条用户输入或一个新 Run 开始，与快照把行分组成消息的规则一致（``_run_messages``）。

    途中到达的 Agent 消息是当前回复的一段，不开回合；Run 第一条 Agent 消息已由 Run 变化切开。
    继承行没有 Run，快照里它们的 Agent 消息都开回合，这里跟着切。

    继承行都没有 Run，所以来源里唤醒开出的回复在分支里并进上一条回复，切点也跟着界面走。
    两处不共用代码，由「每条回复分叉后内容等于到它为止的对话」的契约测试守着。
    """
    return row.run_id != previous.run_id or (
        _is_input(row)
        and (row.run_id is None or cast(UserMessageMetadata, row.metadata).source is None)
    )


def locate_reply(
    rows: list[ConversationTaskContextRecord], reply: ForkReply
) -> ConversationTaskContextRecord:
    """返回这条回复所在回合的首行。

    异常:
        KeyError: 回复不在这个 Task 的保留对话里，映射 404。
    """

    def opens_reply(row: ConversationTaskContextRecord) -> bool:
        if reply.input_message_id is None:
            return row.run_id == reply.run_id
        return row.message_id == reply.input_message_id and _is_input(row)

    start = next((r for r in _conversation(rows) if opens_reply(r)), None)
    if start is None:
        raise KeyError(reply)
    return start


def next_turn_sequence(
    rows: list[ConversationTaskContextRecord], start: ConversationTaskContextRecord
) -> int | None:
    """``start`` 这一回合之后下一回合的起点序号；这已是最后一回合时返回 ``None``。"""
    conversation = _conversation(rows)
    index = next(i for i, r in enumerate(conversation) if r.sequence == start.sequence)
    return next(
        (
            row.sequence
            for previous, row in zip(conversation[index:], conversation[index + 1 :], strict=False)
            if _starts_turn(previous, row)
        ),
        None,
    )


def turn_start_of_call(
    rows: list[ConversationTaskContextRecord], run_id: int, call_id: str
) -> int:
    """调用所在回合的起点序号；Agent 分叉切在这里，用户这一轮的话和这一轮的过程都不进分支。

    异常:
        KeyError: 这个 Run 没有发起过这次调用。
    """
    conversation = _conversation(rows)
    index = next(
        (
            i
            for i, r in reversed(list(enumerate(conversation)))
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
    if index is None:
        raise KeyError(call_id)
    while index > 0 and not _starts_turn(conversation[index - 1], conversation[index]):
        index -= 1
    return conversation[index].sequence


def launch_of_run(
    source: TaskRecord, run_id: int | None, runs: list[ConversationRunRecord]
) -> TaskForkLaunch:
    """本地回合取它所在 Run 的发起配置，继承来的回合取分支自己的初始配置。"""
    if run_id is None:
        return cast(TaskFork, source.extra.fork).initial_launch.model_copy(deep=True)
    run = next(r for r in runs if r.id == run_id)
    return TaskForkLaunch(
        agent_id=run.agent_id, model_config_id=run.model_config_id, options=run.options
    )


def _visible_in_manual_fork(row: ConversationTaskContextRecord) -> bool:
    """Restore conversation content; internal user messages never become chat bubbles.

    Production hidden rows are internal user kinds (notice, decision, tool results/images).
    An Agent fork additionally hides normal inputs and assistant/tool rows. Their existing
    role/kind identifies what a manual branch can show, without saving another visibility bit.
    """
    if row.message["role"] == "user":
        return (
            isinstance(row.metadata, UserMessageMetadata) and row.metadata.kind in TURN_INPUT_KINDS
        )
    return row.message["role"] in {"assistant", "tool"}


def _not_inherited_metadata() -> ToolMessageMetadata:
    return ToolMessageMetadata(status="cancelled", error=_NOT_INHERITED_HINT)


def _unresolved_calls(
    rows: list[ConversationTaskContextRecord], before_sequence: int
) -> frozenset[str]:
    """占位在切点前、结论不在切点前的调用。

    占位正文只是「已提交」「等待决定」：还在等的占位没有结论，已结算的占位结论写在后面的结果消息
    里（占位只更新展示状态）。这两种原样继承，分支里的卡片永远转圈，模型也以为工作还在进行。
    """
    answered_later = {
        r.metadata.tool_call_id
        for r in rows
        if r.sequence >= before_sequence
        and isinstance(r.metadata, UserMessageMetadata)
        and r.metadata.tool_call_id is not None
    }
    waiting = {
        str(r.message["tool_call_id"])
        for r in rows
        if r.sequence < before_sequence
        and isinstance(r.metadata, ToolMessageMetadata)
        and r.metadata.status in {"pending", "running"}
    }
    return frozenset(answered_later | waiting)


def copy_prefix(
    crud: ConversationTaskContextCrud,
    store: ContextStore,
    session: Session,
    source: TaskRecord,
    target: TaskRecord,
    rows: list[ConversationTaskContextRecord],
    before_sequence: int,
) -> int:
    """复制切点之前的保留行，切点两侧被切开的调用在这里收尾。

    缺结果的调用补一条未继承结果，配对先按原 Run 身份做，后面同 id 的结果才不会顶替前一个 Run
    缺失的那条；孤立或重复的结果中止调用方的事务。继承行收不到执行更新，``run_id`` 为空已表达这
    一点。不复制来源 Run 的状态、用量与耗时。
    """
    prefix = [r for r in rows if r.retained and r.sequence < before_sequence]
    system = next((r for r in prefix if r.sequence == 0), None)
    if system is None:
        raise RuntimeError(f"task {source.id} has no persisted system prompt")
    store.to_system_prompt_entry(source.id, system)
    entries = [
        ContextEntry(r.message, r.run_id, r.sequence, r.message_id, r.metadata)
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
    unresolved = _unresolved_calls(rows, before_sequence)
    hide = target.task_type == TaskType.AGENT_FORK
    restore = source.task_type == TaskType.AGENT_FORK
    result: list[ConversationTaskContextRecord] = []
    for row in prefix:
        project = row.project_to_transport
        if hide:
            project = False
        elif restore and row.run_id is None:
            project = _visible_in_manual_fork(row)
        settled = row.message["role"] == "tool" and row.message["tool_call_id"] in unresolved
        result.append(
            replace(
                row,
                id=None,
                task_id=target.id,
                run_id=None,
                message=(
                    {**row.message, "content": _NOT_INHERITED_CONTENT}
                    if settled
                    else deepcopy(row.message)
                ),
                metadata=_not_inherited_metadata() if settled else deepcopy(row.metadata),
                project_to_transport=project,
            )
        )
        result.extend(repairs.get(row.sequence, []))
    for sequence, row in enumerate(result):
        crud.create(replace(row, sequence=sequence), session=session)
    return len(result)
