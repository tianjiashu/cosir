from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

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
    ) -> None:
        self.appended.append((task_id, run_id, message, seq, include_in_context))
        self.current_max_sequence = seq


def _manager(context_service: _ContextService) -> RuntimeContextManager:
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
    manager._message_sequence = 0
    manager._listeners = []
    manager.have_change = False
    manager._context_revision = 0
    return manager


class _RecordingListener:
    main_agent_only = False
    order = 0

    def __init__(self) -> None:
        self.events = []

    def listen(self, event, result) -> None:
        self.events.append(event)


def test_runtime_context_manager_owns_next_sequence_after_run_restart(monkeypatch) -> None:
    context_service = _ContextService(max_sequence=1)
    manager = _manager(context_service)
    monkeypatch.setattr(
        "app.core.context.runtime_context_manager.CapabilityService.get_model_context_window",
        lambda _model_name: 8192,
    )

    manager.begin_run(SimpleNamespace(task_id=7, id=2, model_name="deepseek-v4-flash"))
    manager.add_message(HumanMessage(content="second message"))

    assert manager._message_sequence == 3
    assert context_service.appended[0][3] == 2


def test_runtime_context_manager_resume_keeps_persisted_run_entries(monkeypatch) -> None:
    context_service = _ContextService(max_sequence=4)
    context_service.loaded = [
        ContextEntry(HumanMessage(content="existing"), 2, 3),
    ]
    manager = _manager(context_service)
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


def test_runtime_context_manager_resume_reprojects_context_window(monkeypatch) -> None:
    context_service = _ContextService(max_sequence=4)
    context_service.loaded = [
        ContextEntry(HumanMessage(content="existing context"), 2, 3),
    ]
    manager = _manager(context_service)
    listener = _RecordingListener()
    manager.add_change_listener(listener)
    monkeypatch.setattr(
        "app.core.context.runtime_context_manager.CapabilityService.get_model_context_window",
        lambda _model_name: 8192,
    )

    manager.begin_run(
        SimpleNamespace(task_id=7, id=2, model_name="deepseek-v4-flash"),
        execution_mode="resume",
    )

    assert len(listener.events) == 1
    assert listener.events[0].type.value == "load_history"
    assert listener.events[0].total_tokens == 8192
    assert listener.events[0].context_revision == 1


def test_load_message_repairs_legacy_system_message_between_tool_messages() -> None:
    """历史坏顺序应在模型输入中恢复为 AI -> Tool* -> System。"""

    context_service = _ContextService(max_sequence=3)
    context_service.loaded = [
        ContextEntry(
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "args": {}, "id": "call-1"}],
            ),
            1,
            1,
        ),
        ContextEntry(SystemMessage(content="repair"), 1, 2),
        ContextEntry(ToolMessage(content="ok", tool_call_id="call-1"), 1, 3),
    ]
    manager = _manager(context_service)
    manager._entries = list(context_service.loaded)

    messages = manager.load_message()

    assert [type(message) for message in messages] == [
        SystemMessage,
        AIMessage,
        ToolMessage,
        SystemMessage,
    ]
    assert messages[-1].content == "repair"
    assert not manager.have_change


def test_load_message_closes_then_repairs_legacy_missing_tool_result() -> None:
    """历史坏顺序且缺结果时，应先补 ToolMessage 再放置 repair SystemMessage。"""

    context_service = _ContextService(max_sequence=2)
    context_service.loaded = [
        ContextEntry(
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "args": {}, "id": "call-1"}],
            ),
            1,
            1,
        ),
        ContextEntry(SystemMessage(content="repair"), 1, 2),
    ]
    manager = _manager(context_service)
    manager._entries = list(context_service.loaded)
    manager.current_run_id = 1
    manager._message_sequence = 3

    messages = manager.load_message()

    assert [type(message) for message in messages] == [
        SystemMessage,
        AIMessage,
        ToolMessage,
        SystemMessage,
    ]
    assert messages[2].tool_call_id == "call-1"
    assert messages[3].content == "repair"


def test_runtime_context_message_writes_are_idempotent_for_recovery() -> None:
    """恢复重放不得重复追加同一 tool result 或 repair prompt。"""

    context_service = _ContextService(max_sequence=2)
    manager = _manager(context_service)
    manager.current_run_id = 1
    existing_tool = ToolMessage(content="ok", tool_call_id="call-1")
    existing_repair = SystemMessage(
        content="repair",
        additional_kwargs={"cosir_message_kind": "tool_call_repair"},
    )
    manager._entries = [
        ContextEntry(existing_tool, 1, 1),
        ContextEntry(existing_repair, 1, 2),
    ]
    manager._message_sequence = 3

    manager.add_message(ToolMessage(content="replayed", tool_call_id="call-1"))
    manager.add_message(
        SystemMessage(
            content="repair",
            additional_kwargs={"cosir_message_kind": "tool_call_repair"},
        )
    )

    assert len(context_service.appended) == 0
    assert manager._entries == [
        ContextEntry(existing_tool, 1, 1),
        ContextEntry(existing_repair, 1, 2),
    ]
