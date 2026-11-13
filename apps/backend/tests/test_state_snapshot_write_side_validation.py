"""快照契约守在写入侧：物化时整份校验、变更按触及范围校验；读取与发布只裁窗口、只拷贝窗口，不校验。"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.assistant_transport.service import conversation_state_service as state_module
from app.assistant_transport.service.conversation_state_service import (
    ConversationStateService,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_mutated,
)
from app.config.constant import Constant
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces

SESSION_ID = 7


def _message(index: int, role: str) -> dict[str, Any]:
    return {
        "id": f"{role}-{index}", "role": role, "turn": index // 2 + 1, "inherited": False,
        "outcome": None, "source": None,
        "parts": [{"type": "text", "text": f"text-{index}", "status": "completed"}],
    }


def _history(count: int) -> ConversationStateSnapshot:
    state = empty_snapshot("长对话")
    state["messages"] = cast(Any, [_message(i, "user" if i % 2 == 0 else "assistant") for i in range(count)])
    return state


@pytest.fixture(autouse=True)
def _spaces(monkeypatch: pytest.MonkeyPatch, backend_env: None) -> Any:
    monkeypatch.setattr(
        "app.session_runtime.session_runtime_space.get_session_service",
        lambda: SimpleNamespace(get_session=lambda _id: SimpleNamespace(id=SESSION_ID, workspace_id=3)),
    )
    monkeypatch.setattr(
        "app.session_runtime.session_runtime_space.get_workspace_service",
        lambda: SimpleNamespace(get_workspace=lambda _id: SimpleNamespace(id=3)),
    )
    session_runtime_spaces.close()
    yield
    session_runtime_spaces.close()


def _service_with(state: ConversationStateSnapshot) -> ConversationStateService:
    service = ConversationStateService()
    session_runtime_spaces.get_or_create(SESSION_ID).get_working_snapshot(lambda: state)
    return service


def test_read_copies_only_the_window_and_does_not_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service_with(_history(3 * Constant.Transport.HISTORY_WINDOW_MESSAGES))
    monkeypatch.setattr(state_module, "validate_snapshot", lambda _state: pytest.fail("读取不应校验"))
    copied: list[int] = []
    real_deepcopy = copy.deepcopy

    def counting_deepcopy(value: Any, *args: Any) -> Any:
        if isinstance(value, dict) and "messages" in value:
            copied.append(len(value["messages"]))
        return real_deepcopy(value, *args)

    monkeypatch.setattr(state_module.copy, "deepcopy", counting_deepcopy)

    state = service.get_state(SESSION_ID)

    assert state["messageOffset"] > 0
    assert len(state["messages"]) <= Constant.Transport.HISTORY_WINDOW_MESSAGES + 1
    assert copied == [len(state["messages"])]  # 只拷贝了窗口
    state["messages"][-1]["parts"][0]["text"] = "被调用方改写"
    assert service.get_state(SESSION_ID)["messages"][-1]["parts"][0]["text"] != "被调用方改写"


def test_materialize_validates_the_rebuild_and_does_not_cache_a_violating_one(monkeypatch: pytest.MonkeyPatch) -> None:
    service = ConversationStateService()
    broken = _history(2)
    broken["title"] = ""
    monkeypatch.setattr(service, "_rebuild", lambda _session_id: copy.deepcopy(broken))
    with pytest.raises(ValueError):
        service.get_state(SESSION_ID)
    assert not session_runtime_spaces.get_or_create(SESSION_ID).has_snapshot()


def test_validation_covers_only_what_a_change_touched() -> None:
    state = _history(4)
    # 另一条消息已经违约：只改第 1 条内部时不该去查它（写入侧保证的是「碰过的部分」）。
    state["messages"][3]["turn"] = 0
    inside = ConversationStateMutation("set", ("messages", 1, "parts", 0, "status"), "completed")
    validate_mutated(state, [inside])
    validate_mutated(state, [ConversationStateMutation("append-text", ("messages", 1, "parts", 0, "text"), "!")])
    validate_mutated(state, [ConversationStateMutation("set", ("handoffs",), [])])

    with pytest.raises(ValueError):  # 整条消息替换：与其他消息共用 id 唯一性，整份校验
        validate_mutated(state, [ConversationStateMutation("set", ("messages", 1), state["messages"][1])])
    with pytest.raises(ValueError):  # 碰到的那条违约
        validate_mutated(state, [ConversationStateMutation("set", ("messages", 3, "inherited"), False)])
    state["title"] = ""
    with pytest.raises(ValueError):  # 独立的顶层字段只校验它自己
        validate_mutated(state, [ConversationStateMutation("set", ("title",), "")])
