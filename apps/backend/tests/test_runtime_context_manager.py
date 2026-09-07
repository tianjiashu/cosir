from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from app.core.context.runtime_context_manager import RuntimeContextManager


class _ContextService:
    def __init__(self, max_sequence: int) -> None:
        self.current_max_sequence = max_sequence
        self.appended: list[tuple[int, int | None, object, int, bool]] = []

    def delete_by_run_id(self, task_id: int, run_id: int) -> None:
        return None

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
    manager._system_entry = None
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

