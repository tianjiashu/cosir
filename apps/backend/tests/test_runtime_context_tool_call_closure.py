"""工具协议的完整性、事实保留与重启恢复边界。

真实 SQLite 覆盖配对依据、缺结果占位、结果原地替换和冷加载顺序；无调用依据的结果
不能发给模型，也不能靠丢弃或伪造调用修复。协议错误必须携带稳定定位并保留数据库事实。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar, cast

import pytest
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
)
from sqlalchemy.orm import Session, sessionmaker

from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_snapshot import validate_snapshot
from app.core.agents.agent_profile import AgentProfile
from app.core.context.context_entry import ContextEntry
from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager
from app.core.context.tool_call_closure import ContextProtocolError
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import ToolMessageMetadata
from app.service.conversation_run.conversation_run_service import ConversationRunService
from app.service.conversation_run.conversation_run_state_service import ConversationRunStateService
from app.service.conversation_run.conversation_task_context_service import (
    ConversationTaskContextService,
)
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.engine_cache import create_sqlite_engine
from app.storage.store_engines import initialize_app_schema
from tests.runtime_context_stub import install_runtime_singletons, runtime_stub

_TOOL_STATUS_VALUES = {"pending", "running", "completed", "failed", "cancelled"}

_CrudT = TypeVar("_CrudT")


def _ai_message(*call_ids: str) -> ChatCompletionAssistantMessageParam:
    """构造 SDK 原生 assistant 工具调用。"""

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
        "reasoning_content": None,
    }


def _image_path() -> str:
    """构造合法的 workspace 相对图片路径（文件名 stem 为 64 位十六进制 asset id）。

    ``build_user_input_parts`` 会把它转成 ``cosir-attachment://<asset_id>`` 并按
    ``Constant.Transport.IMAGE_LOCATOR`` 校验，形状不对会直接抛错。
    """

    return f"attachments/{'a' * 64}.png"


def _bind(crud_type: type[_CrudT], factory: sessionmaker[Session]) -> _CrudT:
    """把真实 CRUD 绑定到本用例自己的 SQLite 工厂（与 acceptance 夹具同构）。"""

    crud = crud_type.__new__(crud_type)
    crud._session_factory = factory  # type: ignore[attr-defined]
    return crud


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """提供真实文件型 SQLite、真实 context service/CRUD 与 task 事实。

    Run 行直接写 ``ConversationRunModel``：本文件只验证 context 契约，不依赖 run CRUD
    （其 ``ConversationRunRecord`` 构造参数与 CRUD 当前不一致，属并行重构中间态）。
    """

    engine = create_sqlite_engine(tmp_path / "storage" / "app.sqlite3")
    initialize_app_schema(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    workspaces = _bind(WorkspaceCrud, factory)
    tasks = _bind(TaskCrud, factory)
    runs = _bind(ConversationRunCrud, factory)
    contexts = _bind(ConversationTaskContextCrud, factory)
    context_service = ConversationTaskContextService.__new__(ConversationTaskContextService)
    context_service._crud = contexts
    workspace = workspaces.create("closure", str(tmp_path))
    task = tasks.create(workspace.id, "tool call closure")
    # 快照重建会按工具名查 display 定义；本文件只验证配对/状态契约，给出最小合法
    # presentation（``validate_snapshot`` 要求它是对象），不依赖工具注册表装配。
    monkeypatch.setattr(
        ConversationTaskStateRebuilder,
        "get_tool_display",
        staticmethod(lambda _name: {"verb": "read"}),
    )

    def seed_run(status: str) -> SimpleNamespace:
        """用真实 run CRUD 建一条 Run，并返回快照重建所需的最小 Run 事实。"""

        record = runs.create(task.id, status=status)
        # 快照重建按 ``ConversationRunRecord`` 读取 Run 输入事实，桩必须与真实记录同形。
        return SimpleNamespace(
            id=record.id,
            context_window_total=record.context_window_total,
            status=status,
            end_reason=None,
            usage=None,
            # 快照重建会投影 Run 级受控错误；桩必须与真实记录的字段集保持同形。
            error=None,
        )

    store = SimpleNamespace(
        engine=engine,
        factory=factory,
        context=context_service,
        contexts=contexts,
        runs=runs,
        task=task,
        seed_run=seed_run,
    )
    try:
        yield store
    finally:
        engine.dispose()


def _manager(
    *,
    context_service: object,
    task_id: int,
    current_run_id: int | None,
    entries: list[ContextEntry],
    next_sequence: int,
) -> RuntimeMessageContextManager:
    """按运行时装配口径构造 manager（跳过需要真实 graph 环境才能跑通的 ``__post_init__``）。"""

    manager = object.__new__(RuntimeMessageContextManager)
    manager.current_task_id = task_id
    manager.agent_profile = cast(AgentProfile, SimpleNamespace())
    manager.workspace = SimpleNamespace(id=7, root_path="")
    # 假服务按协议实现 append，不继承真实 service 类型。
    manager.context_service = cast(ConversationTaskContextService, context_service)
    manager.current_run_id = current_run_id
    manager._system_entry = ContextEntry({"role": "system", "content": "system"}, None, -1)
    manager._entries = list(entries)
    manager._message_sequence = next_sequence
    # 与真实 dataclass 字段同形：Run 重置路径会访问流式草稿表。
    return manager


def _rows(env: SimpleNamespace) -> list[ConversationTaskContextRecord]:
    """读取该 task 的全部 context 行（含未纳入上下文的行），按 sequence 升序。"""

    rows = env.contexts.get(env.task.id, include_in_context=False)
    return cast(list[ConversationTaskContextRecord], rows)


def _assert_model_protocol_closed(messages: list[ChatCompletionMessageParam]) -> None:
    """断言模型输入满足 provider 工具协议（忽略首条 system prompt）。"""

    pending: set[str] = set()
    for message in messages[1:]:
        if message["role"] == "assistant":
            pending.update(
                str(call.get("id")) for call in message.get("tool_calls") if call.get("id")
            )
        elif message["role"] == "tool":
            assert (
                message.get("tool_call_id") in pending
            ), f"tool message {message.get('tool_call_id')} 没有前置 assistant tool_call"
            pending.discard(message.get("tool_call_id"))
    assert not pending, f"存在未闭合的 tool_call: {sorted(pending)}"


class _ContractContextService:
    """按 ``ConversationTaskContextService`` 当前契约实现的假服务。

    契约（见 service / CRUD docstring）：``append`` 只追加、恒不返回去重结果——持久化层不再
    知道「工具调用身份」；「该调用是否已有结果行」由 ``RuntimeMessageContextManager`` 依据内存
    working copy（``_entries``）判定，命中时走 ``replace_message`` 原地覆盖。
    """

    def __init__(self) -> None:
        self.appended: list[tuple[int | None, ChatCompletionMessageParam, int]] = []

    def append(
        self,
        task_id: int,
        run_id: int | None,
        message: ChatCompletionMessageParam,
        seq: int,
        include_in_context: bool,
        metadata: object = None,
        project_to_transport: bool = True,
    ) -> None:
        """记录一次追加；不做任何去重。"""

        self.appended.append((run_id, message, seq))
        return ContextEntry(message, run_id, seq)

    def entries_in_context(self, task_id: int) -> list[ContextEntry]:
        """纯内存用例不通过 service 读取历史。"""

        return []

    def max_sequence(self, task_id: int) -> int:
        """纯内存用例不使用序号游标。"""

        return 0

    def delete_by_run_id(self, task_id: int, run_id: int) -> None:
        """纯内存用例不删除行。"""


def test_duplicate_call_id_slots_fail_loudly_instead_of_reusing_a_row(
    env: SimpleNamespace,
) -> None:
    """同一条 AI 消息出现重复 ``call_id``（畸形数据）时缺失槽必须响亮失败。

    第一个槽会认领既有结果行，第二个同 id 槽按配对规则是「缺失」；此时若静默复用旧行，后续按索引
    取新条目会失配，因此显式抛 ``RuntimeError`` 而不是悄悄改写别人的结果。
    """

    run = env.seed_run("running")
    duplicate_calls = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({})},
            },
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({})},
            },
        ],
    }
    existing_result = {"role": "tool", "content": "result", "tool_call_id": "call-1"}
    env.context.append(env.task.id, run.id, duplicate_calls, 1, True)
    env.context.append(
        env.task.id, run.id, existing_result, 2, True, ToolMessageMetadata(status="completed")
    )
    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=[
            ContextEntry(duplicate_calls, run.id, 1),
            ContextEntry(existing_result, run.id, 2),
        ],
        next_sequence=3,
    )

    with pytest.raises(RuntimeError):
        manager.load_message()


def test_placeholder_belongs_to_the_run_that_requested_the_tool(env: SimpleNamespace) -> None:
    """取消后发新消息：占位归属原 Run，重建按 Run 分组仍能配对成功。

    形状与生产一致（未闭合调用是上一个 Run 的**最后几个**工具调用）：run N 的
    ``AIMessage(tool_calls)`` 是它最后一条消息，随后用户发新消息 → run N+1 的
    ``HumanMessage`` 在 Run 准备阶段先落库（``WorkflowSession``），
    再进入 run N+1 的 ``model_node`` 取数，此时才补占位。
    """

    previous_run = env.seed_run("cancelled")
    current_run = env.seed_run("completed")
    ai_message = _ai_message("call-1")
    human_message = {"role": "user", "content": "继续"}
    env.context.append(env.task.id, previous_run.id, ai_message, 1, True)
    env.context.append(env.task.id, current_run.id, human_message, 2, True)

    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=current_run.id,
        entries=[
            ContextEntry(ai_message, previous_run.id, 1),
            ContextEntry(human_message, current_run.id, 2),
        ],
        next_sequence=3,
    )
    messages = manager.load_message()

    # 1) 模型输入闭合且顺序正确（占位插在 AI 之后、下一 Run 的 Human 之前）。
    assert [message["role"] for message in messages] == [
        "system",
        "assistant",
        "tool",
        "user",
    ]
    _assert_model_protocol_closed(messages)

    rows = _rows(env)
    placeholders = [row for row in rows if row.message["role"] == "tool"]
    assert len(placeholders) == 1
    placeholder = placeholders[0]
    human_row = next(row for row in rows if row.message["role"] == "user")

    # 2) 占位归属**发起调用的** Run，而不是恰好正在执行的新 Run。
    assert placeholder.run_id == previous_run.id != current_run.id
    # 3) 占位随行写入 Transport 终态与合法状态。
    placeholder_metadata: object = placeholder.metadata
    assert placeholder_metadata == ToolMessageMetadata.model_validate({"status": "cancelled"})
    # 4) 占位序号全局递增，因此落库顺序仍在下一 Run 的 Human 之后（模型输入靠内存重排修正）。
    assert placeholder.sequence > human_row.sequence

    # 5) 按 Run 分组配对仍成立：两个 Run 分组都能重建并通过快照校验。
    rebuilt = ConversationTaskStateRebuilder.rebuild(env.task, [previous_run, current_run], rows)
    validate_snapshot(rebuilt)
    tool_parts = ConversationTaskStateRebuilder.build_pair_tool_part(rows)
    assert tool_parts["call-1"]["status"] == "cancelled"


def test_same_run_placeholder_metadata_backs_snapshot_rebuild(env: SimpleNamespace) -> None:
    """同 run 尾部的占位带 Transport 终态后，快照重建必须成功且状态合法。

    形状：未闭合调用是该 run 的最后几个工具调用，且取数发生在**同一个 run**
    （``observation_node`` 缺失结果 / 延迟修复两条恢复路径）；此时占位追加在末尾，
    DB 顺序与模型输入顺序天然一致，重排分支不参与。
    """

    run = env.seed_run("cancelled")
    ai_message = _ai_message("call-1")
    env.context.append(env.task.id, run.id, ai_message, 1, True)

    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=[ContextEntry(ai_message, run.id, 1)],
        next_sequence=2,
    )
    messages = manager.load_message()

    rows = _rows(env)
    assert [row.metadata for row in rows if row.message["role"] == "tool"] == [
        ToolMessageMetadata(status="cancelled")
    ]
    # 同 run + 尾部未闭合 = 落库顺序即模型输入顺序。
    assert [row.message["role"] for row in rows] == [message["role"] for message in messages[1:]]

    # 该 metadata 的直接消费者：重建入口取 status 不再崩，且产出合法 wire 终态。
    tool_parts = ConversationTaskStateRebuilder.build_pair_tool_part(rows)
    assert tool_parts["call-1"]["status"] == "cancelled"
    assert tool_parts["call-1"]["status"] in _TOOL_STATUS_VALUES

    rebuilt = ConversationTaskStateRebuilder.rebuild(env.task, [run], rows)
    validate_snapshot(rebuilt)


@pytest.mark.parametrize("later_call", [False, True])
def test_orphan_result_fails_before_any_repair_and_preserves_facts(env, caplog, later_call):
    """后出现同 ID 调用也不能倒配；失败不补占位、不改内容、身份、标记或序号。"""
    run = env.seed_run("running")
    result = {"role": "tool", "content": "外部文件已写入", "tool_call_id": "call-ghost"}
    env.context.append(env.task.id, run.id, result, 1, True, {"status": "completed"})
    if later_call:
        env.context.append(env.task.id, run.id, _ai_message("call-ghost"), 2, True)
    original = _rows(env)
    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=env.context.entries_in_context(env.task.id),
        next_sequence=len(original) + 1,
    )
    for _ in range(2):
        with pytest.raises(ContextProtocolError) as caught:
            manager.load_message()
        assert caught.value.reason == "orphan_tool_result"
        assert caught.value.entry.message == result
        assert _rows(env) == original
        assert manager._entries == env.context.entries_in_context(env.task.id)
    record = next(r for r in caplog.records if r.msg == "runtime_context_tool_protocol_invalid")
    assert record.data["task_id"] == env.task.id
    assert record.data["context_run_id"] == run.id
    assert record.data["message_id"] == original[0].message_id
    assert record.data["sequence"] == 1
    assert record.data["tool_call_id"] == "call-ghost"


@pytest.mark.parametrize("case", ["no_call", "wrong_run", "unknown_id"])
def test_new_tool_result_requires_current_call_before_writing(env, case):
    """没有正确调用依据的新结果不能进入 context，错误保留输入以便调用方定位。"""
    run = env.seed_run("running")
    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=[],
        next_sequence=1,
    )
    if case != "no_call":
        manager.add_message(_ai_message("call-1"))
    if case == "wrong_run":
        manager.current_run_id = env.seed_run("running").id
    original = _rows(env)
    message = {
        "role": "tool",
        "content": "result",
        "tool_call_id": "unknown" if case == "unknown_id" else "call-1",
    }
    with pytest.raises(ContextProtocolError) as caught:
        manager.add_message(
            message, metadata=ToolMessageMetadata.model_validate({"status": "completed"})
        )
    assert caught.value.reason == "tool_result_without_current_call"
    assert caught.value.entry.message == message
    assert _rows(env) == original
    assert manager._message_sequence == len(original) + 1


@pytest.mark.parametrize("case", ["wrong_run", "duplicate"])
def test_persisted_result_must_have_one_owner_in_the_same_run(env, case):
    run = env.seed_run("running")
    other = env.seed_run("running")
    env.context.append(env.task.id, run.id, _ai_message("call-1"), 1, True)
    result = {"role": "tool", "content": "result", "tool_call_id": "call-1"}
    env.context.append(
        env.task.id,
        other.id if case == "wrong_run" else run.id,
        result,
        2,
        True,
        ToolMessageMetadata(status="completed"),
    )
    if case == "duplicate":
        env.context.append(
            env.task.id, run.id, result, 3, True, ToolMessageMetadata(status="completed")
        )
    original = _rows(env)
    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=env.context.entries_in_context(env.task.id),
        next_sequence=len(original) + 1,
    )
    with pytest.raises(ContextProtocolError) as caught:
        manager.load_message()
    assert caught.value.reason == (
        "orphan_tool_result" if case == "wrong_run" else "duplicate_tool_result"
    )
    assert _rows(env) == original


def test_placeholder_does_not_block_later_real_tool_result(env: SimpleNamespace) -> None:
    """占位不得阻塞同一调用真实结果的写入：真实结果原地覆盖占位，只留一行。

    测试直接写入取消占位与真实结果，验证 ``add_message`` 按调用身份原地覆盖，
    不新增行或推进序号；普通 Run 恢复不会重放已执行工具。
    """

    run = env.seed_run("running")
    ai_message = _ai_message("call-1")
    env.context.append(env.task.id, run.id, ai_message, 1, True)
    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=[ContextEntry(ai_message, run.id, 1)],
        next_sequence=2,
    )
    manager.load_message()

    outcome = manager.add_message(
        {"role": "tool", "content": "real tool output", "tool_call_id": "call-1"},
        metadata=ToolMessageMetadata.model_validate({"status": "completed"}),
    )

    assert outcome == "replaced"
    rows = _rows(env)
    result_rows = [row for row in rows if row.message["role"] == "tool"]
    assert len(result_rows) == 1, "真实结果必须覆盖占位，不得并排留下第二行"
    assert result_rows[0].sequence == 2, "覆盖复用占位已占用的序号"
    assert result_rows[0].message.get("content") == "real tool output"
    assert result_rows[0].metadata == ToolMessageMetadata.model_validate({"status": "completed"})

    messages = manager.load_message()
    _assert_model_protocol_closed(messages)
    modeled = [message for message in messages if message["role"] == "tool"]
    assert [message.get("content") for message in modeled] == ["real tool output"]


def test_close_unclosed_tool_call_is_idempotent_and_ordered(env: SimpleNamespace) -> None:
    """正常路径：占位补齐一次、位置正确，二次取数不再新增行。"""

    run = env.seed_run("running")
    first_call = _ai_message("call-1")
    second_call = _ai_message("call-2", "call-3")
    first_result = {"role": "tool", "content": "a done", "tool_call_id": "call-1"}
    third_result = {"role": "tool", "content": "c done", "tool_call_id": "call-3"}
    env.context.append(env.task.id, run.id, first_call, 1, True)
    env.context.append(
        env.task.id, run.id, first_result, 2, True, ToolMessageMetadata(status="completed")
    )
    env.context.append(env.task.id, run.id, second_call, 3, True)
    env.context.append(env.task.id, run.id, third_result, 4, True, {"status": "completed"})

    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=[
            ContextEntry(first_call, run.id, 1),
            ContextEntry(first_result, run.id, 2),
            ContextEntry(second_call, run.id, 3),
            ContextEntry(third_result, run.id, 4),
        ],
        next_sequence=5,
    )

    messages = manager.load_message()

    tool_ids = [message.get("tool_call_id") for message in messages if message["role"] == "tool"]
    assert tool_ids == ["call-1", "call-2", "call-3"]
    _assert_model_protocol_closed(messages)
    assert len([row for row in _rows(env) if row.message["role"] == "tool"]) == 3

    manager._entries = [
        ContextEntry(message, run.id, index) for index, message in enumerate(messages[1:], start=1)
    ]
    manager.load_message()

    assert len([row for row in _rows(env) if row.message["role"] == "tool"]) == 3


def test_close_keeps_completed_batches_and_only_appends_missing_results(
    env: SimpleNamespace,
) -> None:
    """多批次共用配对计划：已完成批次不写库，只有缺失结果才补占位。"""

    run = env.seed_run("running")
    first_call = _ai_message("call-1")
    first_result = {"role": "tool", "content": "a done", "tool_call_id": "call-1"}
    second_call = _ai_message("call-2", "call-3")
    second_result = {"role": "tool", "content": "b done", "tool_call_id": "call-2"}
    env.context.append(env.task.id, run.id, first_call, 1, True)
    env.context.append(
        env.task.id, run.id, first_result, 2, True, ToolMessageMetadata(status="completed")
    )
    env.context.append(env.task.id, run.id, second_call, 3, True)
    env.context.append(env.task.id, run.id, second_result, 4, True, {"status": "completed"})

    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=[
            ContextEntry(first_call, run.id, 1),
            ContextEntry(first_result, run.id, 2),
            ContextEntry(second_call, run.id, 3),
            ContextEntry(second_result, run.id, 4),
        ],
        next_sequence=5,
    )

    messages = manager.load_message()

    # 只补 call-3 一条占位，且顺序仍是 AI -> Tool×3。
    assert [message.get("tool_call_id") for message in messages if message["role"] == "tool"] == [
        "call-1",
        "call-2",
        "call-3",
    ]
    _assert_model_protocol_closed(messages)
    rows = _rows(env)
    assert [row.message["role"] for row in rows] == [
        "assistant",
        "tool",
        "assistant",
        "tool",
        "tool",
    ]
    placeholders = [
        row
        for row in rows
        if row.message["role"] == "tool" and row.message.get("tool_call_id") == "call-3"
    ]
    assert len(placeholders) == 1
    assert placeholders[0].run_id == run.id
    assert placeholders[0].metadata == ToolMessageMetadata.model_validate({"status": "cancelled"})
    # 更早批次（call-1）的结果行原样保留，没有被重写或搬动。
    assert (
        next(
            row.sequence
            for row in rows
            if row.message["role"] == "tool" and row.message.get("tool_call_id") == "call-1"
        )
        == 2
    )


def test_recover_orphaned_runs_cancels_run_and_closes_tool_calls(env: SimpleNamespace) -> None:
    """重启恢复：active Run 收口为取消，并补齐其最后一个 AI 消息上缺失结果的调用。"""

    run = env.seed_run("running")
    ai_message = _ai_message("call-1", "call-2")
    settled = {"role": "tool", "content": "a done", "tool_call_id": "call-1"}
    human = {"role": "user", "content": "user input"}
    env.context.append(env.task.id, run.id, human, 1, True)
    env.context.append(env.task.id, run.id, ai_message, 2, True)
    env.context.append(env.task.id, run.id, settled, 3, True, {"status": "completed"})

    run_crud = _bind(ConversationRunCrud, env.factory)
    service = ConversationRunService.__new__(ConversationRunService)
    service._run = run_crud
    service._context = env.context
    service._session_factory = env.factory
    service._run_state = ConversationRunStateService.__new__(ConversationRunStateService)
    service._run_state._run = run_crud

    recovered = service.recover_orphaned_runs()

    assert [record.id for record in recovered] == [run.id]
    # 幂等：Run 已不是 active，重复调用既不返回也不重复写。
    assert service.recover_orphaned_runs() == []
    persisted = run_crud.get(run.id)
    assert persisted.status == "cancelled"
    assert persisted.end_reason == "runtime_restarted"

    rows = _rows(env)
    tool_rows = [row for row in rows if row.message["role"] == "tool"]
    assert [(row.message.get("tool_call_id"), row.run_id) for row in tool_rows] == [
        ("call-1", run.id),
        ("call-2", run.id),
    ]
    # 只补缺失项：已有结果行原样保留，新增占位带取消终态与调用所属 Run。
    assert tool_rows[0].metadata == ToolMessageMetadata.model_validate({"status": "completed"})
    placeholder = tool_rows[1]
    assert placeholder.metadata == ToolMessageMetadata.model_validate({"status": "cancelled"})
    assert placeholder.run_id == run.id

    # 冷读：Run 终态与工具 part 终态对齐，快照无需额外投影即可通过校验。
    rebuilt = ConversationTaskStateRebuilder.rebuild(env.task, [run], rows)
    validate_snapshot(rebuilt)
    tool_parts = ConversationTaskStateRebuilder.build_pair_tool_part(rows)
    assert {call_id: part["status"] for call_id, part in tool_parts.items()} == {
        "call-1": "completed",
        "call-2": "cancelled",
    }


def test_recover_orphaned_runs_preserves_waiting_run(env: SimpleNamespace) -> None:
    """重启只收敛失去执行者的 running；waiting 留待用户提交决定。"""

    waiting = env.seed_run("waiting_for_input")
    running = env.seed_run("running")

    run_crud = _bind(ConversationRunCrud, env.factory)
    service = ConversationRunService.__new__(ConversationRunService)
    service._run = run_crud
    service._context = env.context
    service._session_factory = env.factory
    service._run_state = ConversationRunStateService.__new__(ConversationRunStateService)
    service._run_state._run = run_crud

    recovered = service.recover_orphaned_runs()

    assert [record.id for record in recovered] == [running.id]
    assert run_crud.get(running.id).status == "cancelled"
    assert run_crud.get(waiting.id).status == "waiting_for_input"
    assert run_crud.get(waiting.id).end_reason is None
    assert [row for row in _rows(env) if row.run_id == waiting.id] == []
    # 幂等：重复恢复不再把 waiting 收进来。
    assert service.recover_orphaned_runs() == []


def test_cold_reload_normalizes_earlier_batches_without_rewriting_facts(env):
    """修复后继续产生新批次，再冷加载仍闭合；历史占位和结果不重复写库。"""
    first = env.seed_run("cancelled")
    current = env.seed_run("running")
    env.context.append(env.task.id, first.id, _ai_message("old-call"), 1, True)
    env.context.append(env.task.id, current.id, {"role": "user", "content": "继续"}, 2, True)
    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=current.id,
        entries=env.context.entries_in_context(env.task.id),
        next_sequence=3,
    )
    manager.load_message()
    manager.add_message(_ai_message("new-call"))
    manager.add_message(
        {"role": "tool", "content": "done", "tool_call_id": "new-call"},
        metadata=ToolMessageMetadata(status="completed"),
    )
    original = _rows(env)
    for _ in range(2):
        cold = _manager(
            context_service=env.context,
            task_id=env.task.id,
            current_run_id=current.id,
            entries=env.context.entries_in_context(env.task.id),
            next_sequence=6,
        )
        messages = cold.load_message()
        assert [m["role"] for m in messages] == [
            "system",
            "assistant",
            "tool",
            "user",
            "assistant",
            "tool",
        ]
        _assert_model_protocol_closed(messages)
        assert _rows(env) == original


def test_stream_cancelled_preview_stays_display_only_after_reload(env, monkeypatch):
    """流式取消产生的展示占位不是执行结果，不得提升为模型上下文的孤儿 tool。"""
    from openai.types.chat import ParsedFunctionToolCall

    import app.core.workflow.node_helper.streaming_part_state_machine as machine_module
    from app.core.events import ToolCallStatusChangedEvent
    from app.core.workflow.execution_state.tool_batch import ToolBatch
    from app.core.workflow.node_helper import StreamingPartStateMachine
    from tests.sdk_fixtures import sdk_completion

    run = env.seed_run("running")
    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=[],
        next_sequence=1,
    )
    completion = sdk_completion(tool_calls=_ai_message("preview")["tool_calls"])
    manager.save_model_response(
        completion, include_in_context=False, registered_tool_names={"read_file"}
    )
    events: list[object] = []
    monkeypatch.setattr(machine_module, "_message_context", lambda: manager)
    runtime = SimpleNamespace(
        tool_definitions={"read_file": SimpleNamespace(name="read_file", display=None)},
        allows_tools=frozenset({"read_file"}),
        batch=ToolBatch(),
    )
    monkeypatch.setattr(machine_module, "_runtime_context", lambda: runtime)
    install_runtime_singletons(runtime)
    monkeypatch.setattr(machine_module, "emit", events.append)
    machine = StreamingPartStateMachine(
        task_id=env.task.id,
        run_id=run.id,
        step_id="step-1",
    )
    machine.tool_call(
        [
            ParsedFunctionToolCall(
                id="preview",
                type="function",
                function={"name": "read_file", "arguments": json.dumps({})},
            )
        ]
    )
    machine.cancel()
    original = _rows(env)
    assert [row.message["role"] for row in original] == ["assistant", "tool"]
    assert all(not row.include_in_context for row in original)
    assert [
        (event.tool_call_id, event.status)
        for event in events
        if isinstance(event, ToolCallStatusChangedEvent)
    ] == [("preview", "cancelled")]
    cold = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=env.context.entries_in_context(env.task.id),
        next_sequence=3,
    )
    assert [m["role"] for m in cold.load_message()] == ["system"]
    assert _rows(env) == original


def test_recovery_keeps_invalid_context_and_waiting_run(env, caplog):
    """无法安全补配对的 running 仍取消；独立 waiting 保留原请求。"""
    broken = env.seed_run("running")
    healthy = env.seed_run("waiting_for_input")
    env.context.append(
        env.task.id,
        broken.id,
        {"role": "tool", "content": "已执行", "tool_call_id": "ghost"},
        1,
        True,
        metadata=ToolMessageMetadata(status="completed"),
    )
    env.context.append(env.task.id, healthy.id, _ai_message("pending"), 2, True)
    original = _rows(env)[0]
    service = ConversationRunService.__new__(ConversationRunService)
    service._run = _bind(ConversationRunCrud, env.factory)
    service._context = env.context
    service._session_factory = env.factory
    service._run_state = ConversationRunStateService.__new__(ConversationRunStateService)
    service._run_state._run = service._run
    recovered = service.recover_orphaned_runs()
    assert [run.id for run in recovered] == [broken.id]
    assert service._run.get(healthy.id).status == "waiting_for_input"
    assert _rows(env)[0] == original
    assert _rows(env)[-1].message["tool_calls"][0]["id"] == "pending"
    assert service.recover_orphaned_runs() == []
    assert any(r.msg == "run_recovery_context_protocol_invalid" for r in caplog.records)


@pytest.mark.asyncio
async def test_orphan_context_blocks_model_request_and_inbox_consumption(env, monkeypatch):
    """在模型请求和 inbox 消费前失败，原始结果与未消费输入都保持原状。"""
    from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
    from app.core.workflow.execution_state.react_route import ReactRoute
    from app.core.workflow.nodes import model_node
    from app.models.conversation_input import ConversationInput, QueuedTaskInput
    from app.models.conversation_run_options import ConversationRunOptions
    from app.service.conversation_run.task_input_service import TaskInputService
    from app.storage.crud.task_input_crud import TaskInputCrud
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    run = env.seed_run("running")
    env.context.append(
        env.task.id,
        run.id,
        {"role": "tool", "content": "已执行", "tool_call_id": "ghost"},
        1,
        True,
        metadata=ToolMessageMetadata(status="completed"),
    )
    with env.factory.begin() as session:
        TaskInputCrud().append(
            session,
            env.task.id,
            QueuedTaskInput(
                message=ConversationInput(input_text="新消息", display_text="新消息"),
                model_config_id=1,
                options=ConversationRunOptions(),
            ),
        )
    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=env.context.entries_in_context(env.task.id),
        next_sequence=2,
    )
    with pytest.raises(ContextProtocolError):
        manager.consume_inbox()
    requested = []

    async def stream(*_args, **_kwargs):
        requested.append(True)
        yield None

    rc = runtime_stub(
        step_limit=10,
        model=SimpleNamespace(stream=stream),
        task=env.task,
        run=run,
        inbox=TaskInputService(),
        task_space=SimpleNamespace(take_runtime_notices=lambda **_kw: None),
    )
    monkeypatch.setattr(model_node, "_runtime_context", lambda: rc)
    monkeypatch.setattr(model_node, "_message_context", lambda: manager)
    monkeypatch.setattr(
        task_runtime_spaces,
        "get_or_create",
        lambda _id: SimpleNamespace(take_runtime_notices=lambda **_kw: None),
    )
    with pytest.raises(ContextProtocolError):
        await model_node._model_node(
            ReactExecutionState(
                step_count=0,
                tool_error_count=0,
                next_node=ReactRoute.MODEL,
            )
        )
    assert requested == []
    with env.factory() as session:
        queued = TaskInputCrud().list_pending(session, env.task.id)
        assert [item.payload["message"]["input_text"] for item in queued] == ["新消息"]
    assert len(_rows(env)) == 1


def test_included_result_cannot_promote_an_incomplete_draft_into_a_call(env):
    """库中即使有同 ID 草稿，也不能把未完成调用提升为已执行依据。"""
    run = env.seed_run("running")
    env.context.append(env.task.id, run.id, _ai_message("partial"), 1, include_in_context=False)
    env.context.append(
        env.task.id,
        run.id,
        {"role": "tool", "content": "result", "tool_call_id": "partial"},
        2,
        True,
        metadata=ToolMessageMetadata(status="completed"),
    )
    original = _rows(env)
    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=env.context.entries_in_context(env.task.id),
        next_sequence=3,
    )
    with pytest.raises(ContextProtocolError, match="orphan_tool_result"):
        manager.load_message()
    assert _rows(env) == original
    assert not _rows(env)[0].include_in_context
