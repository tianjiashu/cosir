from types import SimpleNamespace

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.core.context.context_entry import ContextEntry
from app.core.context.runtime_context_manager import RuntimeContextManager


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
    ) -> bool:
        self.appended.append((task_id, run_id, message, seq, include_in_context))
        self.current_max_sequence = seq
        return True

    def streaming_messages_for_run(self, _task_id: int, _run_id: int) -> list[object]:
        return []

    def replace_streaming_message(self, record: object, **_kwargs: object) -> None:
        self.appended.append((7, 2, record, getattr(record, "sequence", 0), False))


def _manager(context_service: _ContextService, *, next_sequence: int = 0) -> RuntimeContextManager:
    manager = object.__new__(RuntimeContextManager)
    manager.current_task_id = 7
    manager.agent_profile = SimpleNamespace()
    manager.workspace_root = ""
    manager.total_tokens = 0
    manager.used_tokens = 0
    manager.compressor = None
    manager.context_service = context_service
    manager.current_run_id = None
    manager._system_entry = ContextEntry(SystemMessage(content="system"), None, -1)
    manager._entries = []
    manager._message_sequence = next_sequence
    manager._listeners = []
    manager._tool_schemas = ()
    manager._streaming_messages = {}
    return manager


class _RecordingListener:
    main_agent_only = False
    order = 0

    def __init__(self) -> None:
        self.events = []

    def listen(self, event, result) -> None:
        self.events.append(event)


def test_runtime_context_manager_owns_next_sequence_after_run_restart(monkeypatch) -> None:
    """序号游标由 manager 独占：构造时从持久化最大序号推进一步，之后只由写入自增。"""

    context_service = _ContextService(max_sequence=1)
    manager = _manager(context_service, next_sequence=2)
    monkeypatch.setattr(
        "app.core.context.runtime_context_manager.CapabilityService.get_model_context_window",
        lambda _model_name: 8192,
    )

    manager.begin_run(SimpleNamespace(task_id=7, id=2, model_name="deepseek-v4-flash"))
    manager.add_message(HumanMessage(content="second message"))

    assert context_service.appended[0][3] == 2
    assert manager._message_sequence == 3


def test_runtime_context_manager_resume_keeps_persisted_run_entries(monkeypatch) -> None:
    context_service = _ContextService(max_sequence=4)
    context_service.loaded = [
        ContextEntry(HumanMessage(content="existing"), 2, 3),
    ]
    manager = _manager(context_service, next_sequence=5)
    manager._entries = list(context_service.loaded)
    monkeypatch.setattr(
        "app.core.context.runtime_context_manager.CapabilityService.get_model_context_window",
        lambda _model_name: 8192,
    )

    manager.begin_run(
        SimpleNamespace(task_id=7, id=2, model_name="deepseek-v4-flash"),
        execution_mode="resume",
    )

    assert context_service.deleted == []
    assert manager.current_run_id == 2
    assert manager._entries == context_service.loaded
    assert manager._message_sequence == 5


def test_runtime_context_manager_resume_keeps_tool_schemas_without_reprojecting(
    monkeypatch,
) -> None:
    """resume 只装配本 Run 的 tool schema，不在绑定阶段投影事件（占用由写入/冷读对齐）。"""

    context_service = _ContextService(max_sequence=4)
    context_service.loaded = [
        ContextEntry(HumanMessage(content="existing context"), 2, 3),
    ]
    manager = _manager(context_service, next_sequence=5)
    manager._entries = list(context_service.loaded)
    listener = _RecordingListener()
    manager.add_change_listener(listener)
    monkeypatch.setattr(
        "app.core.context.runtime_context_manager.CapabilityService.get_model_context_window",
        lambda _model_name: 8192,
    )

    manager.begin_run(
        SimpleNamespace(task_id=7, id=2, model_name="deepseek-v4-flash"),
        execution_mode="resume",
        tool_schemas=(
            {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {"type": "object", "properties": {}},
            },
        ),
    )

    assert listener.events == []
    assert manager.total_tokens == 8192
    assert manager._tool_schemas[0]["name"] == "read_file"


def test_runtime_context_manager_replaces_tool_schemas_between_runs(monkeypatch) -> None:
    context_service = _ContextService(max_sequence=0)
    manager = _manager(context_service)
    monkeypatch.setattr(
        "app.core.context.runtime_context_manager.CapabilityService.get_model_context_window",
        lambda _model_name: 8192,
    )

    first_schema = {
        "name": "read_file",
        "description": "Read a file.",
        "parameters": {"type": "object", "properties": {}},
    }
    manager.begin_run(
        SimpleNamespace(task_id=7, id=2, model_name="deepseek-v4-flash"),
        tool_schemas=(first_schema,),
    )
    assert manager._tool_schemas[0]["name"] == "read_file"

    manager.begin_run(
        SimpleNamespace(task_id=7, id=3, model_name="deepseek-v4-flash"),
    )

    assert manager._tool_schemas == ()


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


def test_add_message_chunk_reuses_one_partial_sequence_and_finalizes_it() -> None:
    """流式 chunk 只更新一行 partial，收口时才进入模型上下文。"""

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

    manager.flush_message_chunk(stream_id="step-1", mode="complete")

    assert manager._entries[0].message.content == "你好，世界"
    assert manager._streaming_messages == {}


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
    # partial 已落盘：replace_streaming_message 写入记录保持 is_streaming=True
    record = context_service.appended[-1][2]
    assert record.is_streaming is True
    assert record.include_in_context is False
    # 返回截至当前的聚合消息
    assert result is not None and result.content == "你好，世界"



