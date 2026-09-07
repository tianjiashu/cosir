from types import SimpleNamespace

from langchain_core.messages import HumanMessage, SystemMessage

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
    return manager


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
