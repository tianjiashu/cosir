from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.core.context.context_entry import ContextEntry
from app.core.context.runtime_context_manager import RuntimeContextManager
from app.models.conversation_task_context import ConversationTaskContextRecord


class _ContextService:
    def __init__(self, max_sequence: int) -> None:
        self.current_max_sequence = max_sequence
        self.appended: list[tuple[int, int | None, object, int, bool]] = []
        self.loaded: list[ContextEntry] = []
        self.deleted: list[tuple[int, int]] = []

    def delete_by_run_id(self, task_id: int, run_id: int) -> None:
        self.deleted.append((task_id, run_id))

    def entries_in_context(self, task_id: int) -> list[ContextEntry]:
        return list(self.loaded)

    def max_sequence(self, task_id: int) -> int:
        return self.current_max_sequence

    def append(
        self,
        task_id: int,
        run_id: int | None,
        message: object,
        seq: int,
        include_in_context: bool,
        transport_metadata: object = None,
        is_streaming: bool = False,
    ) -> None:
        self.appended.append((task_id, run_id, message, seq, include_in_context))
        self.current_max_sequence = seq

    def streaming_messages_for_run(self, _task_id: int, _run_id: int) -> list[object]:
        return []

    def replace_message(self, record: object, **_kwargs: object) -> None:
        self.appended.append((7, 2, record, getattr(record, "sequence", 0), False))


def _manager(context_service: _ContextService, *, next_sequence: int = 0) -> RuntimeContextManager:
    manager = object.__new__(RuntimeContextManager)
    manager.current_task_id = 7
    manager.agent_profile = SimpleNamespace()
    manager.workspace_root = ""
    manager.compressor = None
    manager.context_service = context_service
    manager.current_run_id = None
    manager._system_entry = ContextEntry(SystemMessage(content="system"), None, -1)
    manager._entries = []
    manager._message_sequence = next_sequence
    manager._streaming_messages = {}
    return manager


def _ai_message(*call_ids: str) -> AIMessage:
    """构造带工具调用的 AI 消息：配对规则以「最后一条携带 ``tool_calls`` 的 AI 消息」为目标。"""

    return AIMessage(
        content="",
        tool_calls=[{"name": "read_file", "args": {}, "id": call_id} for call_id in call_ids],
    )


def test_runtime_context_manager_owns_next_sequence_after_run_restart() -> None:
    """序号游标由 manager 独占：构造时从持久化最大序号推进一步，之后只由写入自增。"""

    context_service = _ContextService(max_sequence=1)
    manager = _manager(context_service, next_sequence=2)
    manager.begin_run(
        SimpleNamespace(task_id=7, id=2),
    )
    manager.add_message(HumanMessage(content="second message"))

    assert context_service.appended[0][3] == 2
    assert manager._message_sequence == 3


def test_runtime_context_manager_resume_keeps_persisted_run_entries() -> None:
    context_service = _ContextService(max_sequence=4)
    context_service.loaded = [
        ContextEntry(HumanMessage(content="existing"), 2, 3),
    ]
    manager = _manager(context_service, next_sequence=5)
    manager._entries = list(context_service.loaded)
    manager.begin_run(
        SimpleNamespace(task_id=7, id=2),
        execution_mode="resume",
    )

    assert context_service.deleted == []
    assert manager.current_run_id == 2
    assert manager._entries == context_service.loaded
    assert manager._message_sequence == 5


def test_load_message_moves_existing_tool_result_before_later_human_message() -> None:
    """取消后追加新输入时，已有 ToolMessage 也必须紧跟原 tool call。"""

    context_service = _ContextService(max_sequence=4)
    context_service.loaded = [
        ContextEntry(
            AIMessage(
                content="",
                tool_calls=[{"name": "execute_terminal", "args": {}, "id": "call-1"}],
            ),
            1,
            1,
        ),
        ContextEntry(HumanMessage(content="continue"), 2, 2),
        ContextEntry(ToolMessage(content="cancelled", tool_call_id="call-1"), 2, 3),
    ]
    manager = _manager(context_service)
    manager._entries = list(context_service.loaded)

    messages = manager.load_message()

    assert [type(message) for message in messages] == [
        SystemMessage,
        AIMessage,
        ToolMessage,
        HumanMessage,
    ]
    assert messages[2].tool_call_id == "call-1"
    assert messages[3].content == "continue"
    assert context_service.appended == []


def test_streaming_draft_is_finalized_in_place_into_one_context_row() -> None:
    """流式 chunk 只更新一行 partial；收口时原地固化同一行，不新增行、不新增序号。"""

    context_service = _ContextService(max_sequence=4)
    manager = _manager(context_service, next_sequence=5)
    manager.current_run_id = 2

    first = manager.add_message_chunk(AIMessageChunk(content="你好"), stream_id="step-1")
    manager.add_message_chunk(AIMessageChunk(content="，世界"), stream_id="step-1")

    assert first.content == "你好"
    assert manager._message_sequence == 6
    assert manager._entries == []
    assert len(context_service.appended) == 1

    manager.flush_message_chunk(stream_id="step-1")
    assert len(context_service.appended) == 2
    assert context_service.appended[0][3] == context_service.appended[1][3] == 5
    # flush 的默认 running 态只落 partial：草稿既不进模型上下文、也不被当成 canonical 消息。
    assert context_service.appended[1][2].is_streaming is True
    assert context_service.appended[1][2].include_in_context is False
    assert manager._entries == []
    # running 保留草稿供后续 chunk 继续累积，并推进刷写进度。
    assert list(manager._streaming_messages) == [(2, "step-1")]
    assert manager._streaming_messages[(2, "step-1")].persisted_text_length == len("你好，世界")

    # 收口（flush 的 finalize 态）一次完成：丢弃内存草稿状态 + 原地写入调用方交付的修订版。
    revised = AIMessage(content="你好，世界", tool_calls=[])
    returned = manager.flush_message_chunk(
        stream_id="step-1", run_id=2, mode="finalize", message=revised
    )

    assert returned is revised
    assert manager._streaming_messages == {}
    # 固化是原地替换草稿行占用的同一 sequence：不新增行、序号游标不推进，消息进入模型上下文。
    finalized = context_service.appended[-1][2]
    assert finalized.sequence == 5
    assert finalized.include_in_context is True
    assert finalized.is_streaming is False
    # 落库的是修订版（而不是内存里的原始聚合结果）。
    assert finalized.message is revised
    assert [entry.sequence for entry in manager._entries] == [5]
    assert manager._entries[0].message is revised
    assert manager._message_sequence == 6


def test_flush_finalize_raises_without_draft() -> None:
    """无草稿时收口立即失败（``KeyError``）：不得伪造消息，也不得复用上一次的草稿。"""

    context_service = _ContextService(max_sequence=4)
    manager = _manager(context_service, next_sequence=5)
    manager.current_run_id = 2

    with pytest.raises(KeyError):
        manager.flush_message_chunk(stream_id="step-1", mode="finalize")

    assert context_service.appended == []
    assert manager._entries == []
    assert manager._message_sequence == 5


def test_flush_rejects_unknown_mode() -> None:
    """未知 ``mode`` 必须报错，不得悄悄落成某一种既有语义。"""

    context_service = _ContextService(max_sequence=4)
    manager = _manager(context_service, next_sequence=5)
    manager.current_run_id = 2
    manager.add_message_chunk(AIMessageChunk(content="半截"), stream_id="step-1")

    with pytest.raises(ValueError):
        manager.flush_message_chunk(stream_id="step-1", mode=cast(Any, "complete"))

    # 报错发生在写入之前：只有首 chunk 建的那一行为 written，草稿仍在。
    assert len(context_service.appended) == 1
    assert list(manager._streaming_messages) == [(2, "step-1")]


def test_flush_rejects_message_outside_finalize_mode() -> None:
    """``message`` 只在 finalize 态有意义：其余模式传入必须报错，不得静默忽略。"""

    context_service = _ContextService(max_sequence=4)
    manager = _manager(context_service, next_sequence=5)
    manager.current_run_id = 2
    manager.add_message_chunk(AIMessageChunk(content="半截"), stream_id="step-1")

    with pytest.raises(ValueError):
        manager.flush_message_chunk(stream_id="step-1", message=AIMessage(content="修订版"))

    # 报错发生在写入之前：内存草稿仍在，且没有发生任何原地写入。
    assert list(manager._streaming_messages) == [(2, "step-1")]
    assert [
        item[2]
        for item in context_service.appended
        if isinstance(item[2], ConversationTaskContextRecord)
    ] == []


def test_add_message_replaces_paired_tool_result_row_in_place() -> None:
    """命中同一条调用的既有结果行时原地覆盖（真实结果顶掉占位），不新增行、不推进序号。"""

    context_service = _ContextService(max_sequence=4)
    manager = _manager(context_service, next_sequence=5)
    manager.current_run_id = 2
    manager.add_message(_ai_message("call-1"))
    manager.add_message(ToolMessage(content="placeholder", tool_call_id="call-1"))

    outcome = manager.add_message(
        ToolMessage(content="real", tool_call_id="call-1"),
        transport_metadata={"status": "completed"},
    )

    assert outcome == "replaced"
    assert len(context_service.appended) == 3
    replaced = context_service.appended[-1][2]
    assert replaced.sequence == 6
    assert replaced.include_in_context is True
    assert replaced.transport_metadata == {"status": "completed"}
    assert [entry.message.content for entry in manager._entries] == ["", "real"]
    assert manager._message_sequence == 7, "覆盖不得推进序号游标"


def test_add_message_appends_when_same_run_reuses_call_id_in_a_later_step() -> None:
    """同一 Run 内跨 model 步复用同一 ``tool_call_id`` 时也必须追加。

    配对只认最后一条携带 ``tool_calls`` 的 ``AIMessage`` 之后的结果行，因此上一 model 步的结果行
    不会被本步的调用命中——这正是「按 id 全表匹配」会踩坏的第二种形态。
    """

    context_service = _ContextService(max_sequence=4)
    manager = _manager(context_service, next_sequence=5)
    manager.current_run_id = 2
    manager.add_message(_ai_message("call-1"))
    manager.add_message(ToolMessage(content="step1 result", tool_call_id="call-1"))
    manager.add_message(_ai_message("call-1"))

    outcome = manager.add_message(ToolMessage(content="step2 result", tool_call_id="call-1"))

    assert outcome == "appended"
    assert [
        item[2]
        for item in context_service.appended
        if isinstance(item[2], ConversationTaskContextRecord)
    ] == [], "上一 model 步的结果行不得被覆盖"
    assert [entry.message.content for entry in manager._entries][-2:] == ["", "step2 result"]


def test_add_message_does_not_overwrite_another_run_row_with_same_tool_call_id() -> None:
    """同一 ``tool_call_id`` 可跨 Run 复用：不得覆盖别的 Run 已有的结果行。

    判据沿用 ``plan_tool_call_closure`` 的配对规则（只认目标 ``AIMessage`` 之后、同 Run 的既有
    结果行），因此「当前 Run 的调用没有结果、别的 Run 用了同一个 id」时必须走追加。
    """

    context_service = _ContextService(max_sequence=4)
    manager = _manager(context_service, next_sequence=5)
    manager.current_run_id = 1
    manager.add_message(_ai_message("call-1"))
    manager.add_message(ToolMessage(content="run1 result", tool_call_id="call-1"))

    # 切到 run 2，模型复用同一 id，并写它的真实结果。
    manager.current_run_id = 2
    manager.add_message(_ai_message("call-1"))

    outcome = manager.add_message(ToolMessage(content="run2 result", tool_call_id="call-1"))

    assert outcome == "appended"
    assert [entry.message.content for entry in manager._entries][2:] == ["", "run2 result"]
    assert [
        item[2]
        for item in context_service.appended
        if isinstance(item[2], ConversationTaskContextRecord)
    ] == [], "不得原地替换（那会覆盖 run 1 的结果行）"


def test_flush_message_chunk_cancel_drops_state_but_persists_partial() -> None:
    """取消 flush 以 partial 落盘并丢弃内存 state，不进入模型上下文、不触发收口。"""

    context_service = _ContextService(max_sequence=4)
    manager = _manager(context_service, next_sequence=5)
    manager.current_run_id = 2

    manager.add_message_chunk(AIMessageChunk(content="你好"), stream_id="step-1")
    manager.add_message_chunk(AIMessageChunk(content="，世界"), stream_id="step-1")
    assert manager._streaming_messages != {}

    result = manager.flush_message_chunk(stream_id="step-1", mode="cancel")

    # 内存 state 已丢弃，run 终止后不会悬挂
    assert manager._streaming_messages == {}
    # 取消是 partial，不收口进模型上下文
    assert manager._entries == []
    # partial 已落盘：replace_message 写入记录保持 is_streaming=True
    record = context_service.appended[-1][2]
    assert record.is_streaming is True
    assert record.include_in_context is False
    # 返回截至当前的聚合消息
    assert result is not None and result.content == "你好，世界"
