import json
from types import SimpleNamespace

import pytest
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
)

from app.core.context.context_entry import ContextEntry
from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import EmptyMessageMetadata, ToolMessageMetadata
from tests.sdk_fixtures import sdk_completion


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
        metadata: object = None,
        project_to_transport: bool = True,
    ) -> ContextEntry:
        self.appended.append((task_id, run_id, message, seq, include_in_context))
        self.current_max_sequence = seq
        return ContextEntry(
            message,
            run_id,
            seq,
            metadata=metadata if metadata is not None else EmptyMessageMetadata(),
        )

    def replace_message(self, record: object, **_kwargs: object) -> None:
        self.appended.append((7, 2, record, getattr(record, "sequence", 0), False))


def _manager(
    context_service: _ContextService, *, next_sequence: int = 0
) -> RuntimeMessageContextManager:
    manager = object.__new__(RuntimeMessageContextManager)
    manager.current_task_id = 7
    manager.agent_profile = SimpleNamespace()
    manager.workspace = SimpleNamespace(id=7, root_path="")
    manager.context_service = context_service
    manager.current_run_id = None
    manager._system_entry = ContextEntry({"role": "system", "content": "system"}, None, -1)
    manager._entries = []
    manager._message_sequence = next_sequence
    manager._draft_response = None
    return manager


def _ai_message(*call_ids: str) -> ChatCompletionAssistantMessageParam:
    """构造带工具调用的 AI 消息：配对规则以「最后一条携带 ``tool_calls`` 的 AI 消息」为目标。"""

    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({})},
            }
            for call_id in call_ids
        ],
    }


def test_runtime_message_context_manager_owns_next_sequence_after_run_restart() -> None:
    """序号游标由 manager 独占：构造时从持久化最大序号推进一步，之后只由写入自增。"""

    context_service = _ContextService(max_sequence=1)
    manager = _manager(context_service, next_sequence=2)
    manager.begin_run(
        SimpleNamespace(task_id=7, id=2),
    )
    manager.add_message({"role": "user", "content": "second message"})

    assert context_service.appended[0][3] == 2
    assert manager._message_sequence == 3


def test_runtime_message_context_manager_resume_keeps_persisted_run_entries() -> None:
    context_service = _ContextService(max_sequence=4)
    context_service.loaded = [
        ContextEntry({"role": "user", "content": "existing"}, 2, 3),
    ]
    manager = _manager(context_service, next_sequence=5)
    manager._entries = list(context_service.loaded)
    manager.begin_run(
        SimpleNamespace(task_id=7, id=2),
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
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "execute_terminal", "arguments": json.dumps({})},
                    }
                ],
            },
            1,
            1,
        ),
        ContextEntry({"role": "user", "content": "continue"}, 2, 2),
        ContextEntry({"role": "tool", "content": "cancelled", "tool_call_id": "call-1"}, 1, 3),
    ]
    manager = _manager(context_service)
    manager._entries = list(context_service.loaded)

    messages = manager.load_message()

    assert [message["role"] for message in messages] == [
        "system",
        "assistant",
        "tool",
        "user",
    ]
    assert messages[2].get("tool_call_id") == "call-1"
    assert messages[3].get("content") == "continue"
    assert context_service.appended == []


@pytest.mark.parametrize("include_in_context", [False, True])
def test_save_model_response_writes_once_and_controls_context_membership(include_in_context):
    """首个响应占用一个序号；只有准入响应进入 working copy。"""
    context_service = _ContextService(max_sequence=4)
    manager = _manager(context_service, next_sequence=5)
    manager.current_run_id = 2
    response = sdk_completion(content="最后收到的内容")
    manager.save_model_response(
        response, include_in_context=include_in_context, registered_tool_names=set()
    )
    assert len(context_service.appended) == 1
    assert context_service.appended[0][2]["content"] == "最后收到的内容"
    assert context_service.appended[0][4] is include_in_context
    assert manager.next_sequence == 6
    assert len(manager._entries) == int(include_in_context)


def test_stream_snapshots_replace_one_identity_until_context_admission():
    service = _ContextService(max_sequence=4)
    manager = _manager(service, next_sequence=5)
    entry = manager.save_model_response(
        sdk_completion(content="part"),
        include_in_context=False,
        registered_tool_names=set(),
    )
    identity = entry.message_id
    entry = manager.save_model_response(
        sdk_completion(content="part tail"),
        include_in_context=False,
        registered_tool_names=set(),
    )
    assert manager.next_sequence == 6
    assert manager._entries == []
    record = service.appended[-1][2]
    assert record.message_id == identity
    assert record.sequence == 5
    assert record.message["content"] == "part tail"
    assert not record.include_in_context
    manager.save_model_response(
        sdk_completion(content="part tail"),
        include_in_context=True,
        registered_tool_names=set(),
    )
    assert service.appended[-1][2].include_in_context
    assert manager._entries == [entry]
    assert manager.next_sequence == 6
    # 提交即结束本次草稿：下一次调用必须新起一行。
    assert manager._draft_response is None


def test_model_response_filters_unknown_calls_before_persistence():
    service = _ContextService(max_sequence=0)
    manager = _manager(service, next_sequence=1)
    manager.current_run_id = 2
    response = sdk_completion(
        tool_calls=[
            {
                "id": "known",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{"},
            },
            {"id": "unknown", "type": "function", "function": {"name": "other", "arguments": "{}"}},
        ]
    )

    entry = manager.save_model_response(
        response, include_in_context=True, registered_tool_names={"read_file"}
    )

    assert [call["id"] for call in entry.message["tool_calls"]] == ["known"]
    assert entry.message["tool_calls"][0]["function"]["arguments"] == "{"


def test_add_message_replaces_paired_tool_result_row_in_place() -> None:
    """命中同一条调用的既有结果行时原地覆盖（真实结果顶掉占位），不新增行、不推进序号。"""

    context_service = _ContextService(max_sequence=4)
    manager = _manager(context_service, next_sequence=5)
    manager.current_run_id = 2
    manager.add_message(_ai_message("call-1"))
    manager.add_message(
        {"role": "tool", "content": "placeholder", "tool_call_id": "call-1"},
        metadata=ToolMessageMetadata(
            status="running",
            display_data={"user_input_request": {"request_id": "wait-1"}},
            error="old",
        ),
    )

    outcome = manager.add_message(
        {"role": "tool", "content": "real", "tool_call_id": "call-1"},
        metadata=ToolMessageMetadata.model_validate({"status": "completed"}),
    )

    assert outcome == "replaced"
    assert len(context_service.appended) == 3
    replaced = context_service.appended[-1][2]
    assert replaced.sequence == 6
    assert replaced.include_in_context is True
    assert replaced.metadata == ToolMessageMetadata.model_validate({"status": "completed"})
    assert [entry.message.get("content") for entry in manager._entries] == ["", "real"]
    assert manager._entries[-1].metadata == ToolMessageMetadata(status="completed")
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
    manager.add_message(
        {"role": "tool", "content": "step1 result", "tool_call_id": "call-1"},
        metadata=ToolMessageMetadata(status="completed"),
    )
    manager.add_message(_ai_message("call-1"))

    outcome = manager.add_message(
        {"role": "tool", "content": "step2 result", "tool_call_id": "call-1"},
        metadata=ToolMessageMetadata(status="completed"),
    )

    assert outcome == "appended"
    assert [
        item[2]
        for item in context_service.appended
        if isinstance(item[2], ConversationTaskContextRecord)
    ] == [], "上一 model 步的结果行不得被覆盖"
    assert [entry.message.get("content") for entry in manager._entries][-2:] == ["", "step2 result"]


def test_add_message_does_not_overwrite_another_run_row_with_same_tool_call_id() -> None:
    """同一 ``tool_call_id`` 可跨 Run 复用：不得覆盖别的 Run 已有的结果行。

    判据沿用 ``plan_tool_call_closures`` 的配对规则（只认目标 ``AIMessage`` 之后、同 Run 的既有
    结果行），因此「当前 Run 的调用没有结果、别的 Run 用了同一个 id」时必须走追加。
    """

    context_service = _ContextService(max_sequence=4)
    manager = _manager(context_service, next_sequence=5)
    manager.current_run_id = 1
    manager.add_message(_ai_message("call-1"))
    manager.add_message(
        {"role": "tool", "content": "run1 result", "tool_call_id": "call-1"},
        metadata=ToolMessageMetadata(status="completed"),
    )

    # 切到 run 2，模型复用同一 id，并写它的真实结果。
    manager.current_run_id = 2
    manager.add_message(_ai_message("call-1"))

    outcome = manager.add_message(
        {"role": "tool", "content": "run2 result", "tool_call_id": "call-1"},
        metadata=ToolMessageMetadata(status="completed"),
    )

    assert outcome == "appended"
    assert [entry.message.get("content") for entry in manager._entries][2:] == ["", "run2 result"]
    assert [
        item[2]
        for item in context_service.appended
        if isinstance(item[2], ConversationTaskContextRecord)
    ] == [], "不得原地替换（那会覆盖 run 1 的结果行）"
