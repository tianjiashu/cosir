"""``RuntimeContextManager._close_unclosed_tool_calls`` 的逻辑缺陷验证。

调用点（``load_message`` 是唯一入口）：

- ``model_node``：每个推理步取上下文前调用（``_model_node`` -> ``load_message``）；
- ``observation_node``：工具观察结果缺失（``missing_call_ids``）与延迟修复提示两条
  恢复路径上调用（见 ``observation_node._observe_node``）；
- ``tools_node`` 执行前取消分支**不写占位**，注释显式声明「由下一次 load_message 兜底」，
  因此取消/崩溃遗留的占位实际落在**下一个 run**（或同 run resume 时的取数时刻）。

本文件同时使用两条证据链：

1. 真实 SQLite + 真实 ``ConversationTaskContextService``：钉住占位的**持久化身份**
   （run_id、sequence、transport_metadata）及其与下游快照重建
   （``ConversationTaskStateRebuilder`` / ``validate_snapshot``）的契约冲突；
2. 按 ``append`` 文档契约实现的假服务：钉住 ``created is False`` 与反向孤儿消息两条
   未覆盖分支。

标注 ``xfail(strict=True)`` 的用例断言的是**期望行为**（当前失败即为缺陷证据）；
一旦修复，用例会 XPASS 失败，提醒删除标记。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import TypeVar, cast

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from sqlalchemy.orm import Session, sessionmaker

from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_snapshot import validate_snapshot
from app.core.agents.agent_profile import AgentProfile
from app.core.context.context_entry import ContextEntry
from app.core.context.runtime_context_manager import RuntimeContextManager
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.service.task.conversation_run_service import ConversationRunService
from app.service.task.conversation_task_context_service import ConversationTaskContextService
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.engine_cache import create_sqlite_engine
from app.storage.init_schema import initialize_app_schema

_TOOL_STATUS_VALUES = {"pending", "running", "completed", "failed", "cancelled"}

_CrudT = TypeVar("_CrudT")


def _ai_message(*call_ids: str) -> AIMessage:
    """构造与运行期同形的 AIMessage（快照重建按 ``additional_kwargs`` 取 reasoning 通道）。"""

    return AIMessage(
        content="",
        tool_calls=[
            {"name": "read_file", "args": {}, "id": call_id} for call_id in call_ids
        ],
        additional_kwargs={"reasoning_content": None},
    )


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

        record = runs.create(task.id, f"input for {status} run", status=status)
        # 快照重建按 ``ConversationRunRecord`` 读取 Run 输入事实，桩必须与真实记录同形。
        return SimpleNamespace(
            id=record.id,
            input_text=record.input_text,
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
) -> RuntimeContextManager:
    """按运行时装配口径构造 manager（跳过需要真实 graph 环境才能跑通的 ``__post_init__``）。"""

    manager = object.__new__(RuntimeContextManager)
    manager.current_task_id = task_id
    manager.agent_profile = cast(AgentProfile, SimpleNamespace())
    manager.workspace_root = ""
    manager.total_tokens = 0
    manager.used_tokens = 0
    manager.compressor = None
    # 假服务按协议实现 append，不继承真实 service 类型。
    manager.context_service = cast(ConversationTaskContextService, context_service)
    manager.current_run_id = current_run_id
    manager.is_fork = False
    manager._system_entry = ContextEntry(SystemMessage(content="system"), None, -1)
    manager._entries = list(entries)
    manager._message_sequence = next_sequence
    manager._listeners = []
    manager._tool_schemas = ()
    # 与真实 dataclass 字段同形：Run 重置路径会访问流式草稿表。
    manager._streaming_messages = {}
    return manager


def _rows(env: SimpleNamespace) -> list[ConversationTaskContextRecord]:
    """读取该 task 的全部 context 行（含未纳入上下文的行），按 sequence 升序。"""

    rows = env.contexts.get(env.task.id, include_in_context=False)
    return cast(list[ConversationTaskContextRecord], rows)


def _assert_model_protocol_closed(messages: list[BaseMessage]) -> None:
    """断言模型输入满足 provider 工具协议（忽略首条 system prompt）。"""

    pending: set[str] = set()
    for message in messages[1:]:
        if isinstance(message, AIMessage):
            pending.update(str(call.get("id")) for call in message.tool_calls if call.get("id"))
        elif isinstance(message, ToolMessage):
            assert message.tool_call_id in pending, (
                f"tool message {message.tool_call_id} 没有前置 assistant tool_call"
            )
            pending.discard(message.tool_call_id)
    assert not pending, f"存在未闭合的 tool_call: {sorted(pending)}"


class _ContractContextService:
    """按 ``ConversationTaskContextService.append`` 文档契约实现的假服务。

    契约（见 service / CRUD docstring）：同一 ``(task_id, run_id, tool_call_id)``
    再次追加时返回 ``False``，调用方不得假定 ``_entries`` 一定新增。
    """

    def __init__(self, existing: set[tuple[int | None, str]] | None = None) -> None:
        self.existing = set(existing or ())
        self.appended: list[tuple[int | None, BaseMessage, int]] = []

    def append(
        self,
        task_id: int,
        run_id: int | None,
        message: BaseMessage,
        seq: int,
        include_in_context: bool,
        transport_metadata: object = None,
    ) -> bool:
        """重复的 tool 结果返回 ``False``；其余情况记录并返回 ``True``。"""

        if isinstance(message, ToolMessage) and message.tool_call_id:
            key = (run_id, message.tool_call_id)
            if key in self.existing:
                return False
            self.existing.add(key)
        self.appended.append((run_id, message, seq))
        return True

    def entries_in_context(self, task_id: int) -> list[ContextEntry]:
        """纯内存用例不通过 service 读取历史。"""

        return []

    def max_sequence(self, task_id: int) -> int:
        """纯内存用例不使用序号游标。"""

        return 0

    def delete_by_run_id(self, task_id: int, run_id: int) -> None:
        """纯内存用例不删除行。"""


def test_placeholder_belongs_to_the_run_that_requested_the_tool(env: SimpleNamespace) -> None:
    """取消后发新消息：占位归属原 Run，重建按 Run 分组仍能配对成功。

    形状与生产一致（未闭合调用是上一个 Run 的**最后几个**工具调用）：run N 的
    ``AIMessage(tool_calls)`` 是它最后一条消息，随后用户发新消息 → run N+1 的
    ``HumanMessage`` 在 Run 准备阶段先落库（``ConversationRunCommandService``），
    再进入 run N+1 的 ``model_node`` 取数，此时才补占位。
    """

    previous_run = env.seed_run("cancelled")
    current_run = env.seed_run("completed")
    ai_message = _ai_message("call-1")
    human_message = HumanMessage(content="继续")
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
    assert [type(message) for message in messages] == [
        SystemMessage,
        AIMessage,
        ToolMessage,
        HumanMessage,
    ]
    _assert_model_protocol_closed(messages)

    rows = _rows(env)
    placeholders = [row for row in rows if isinstance(row.message, ToolMessage)]
    assert len(placeholders) == 1
    placeholder = placeholders[0]
    human_row = next(row for row in rows if isinstance(row.message, HumanMessage))

    # 2) 占位归属**发起调用的** Run，而不是恰好正在执行的新 Run。
    assert placeholder.run_id == previous_run.id != current_run.id
    # 3) 占位随行写入 Transport 终态与合法状态。
    placeholder_metadata: object = placeholder.transport_metadata
    assert placeholder_metadata == {"status": "cancelled"}
    # 4) 占位序号全局递增，因此落库顺序仍在下一 Run 的 Human 之后（模型输入靠内存重排修正）。
    assert placeholder.sequence > human_row.sequence

    # 5) 按 Run 分组配对仍成立：两个 Run 分组都能重建并通过快照校验。
    rebuilt = ConversationTaskStateRebuilder.rebuild(
        env.task, [previous_run, current_run], rows
    )
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
    assert [
        row.transport_metadata for row in rows if isinstance(row.message, ToolMessage)
    ] == [{"status": "cancelled"}]
    # 同 run + 尾部未闭合 = 落库顺序即模型输入顺序。
    assert [type(row.message) for row in rows] == [type(message) for message in messages[1:]]

    # 该 metadata 的直接消费者：重建入口取 status 不再崩，且产出合法 wire 终态。
    tool_parts = ConversationTaskStateRebuilder.build_pair_tool_part(rows)
    assert tool_parts["call-1"]["status"] == "cancelled"
    assert tool_parts["call-1"]["status"] in _TOOL_STATUS_VALUES

    rebuilt = ConversationTaskStateRebuilder.rebuild(env.task, [run], rows)
    validate_snapshot(rebuilt)


def test_close_unclosed_tool_calls_survives_duplicate_append_result() -> None:
    """``append`` 返回 ``False``（同 run 同 tool_call_id 已有行）时不得崩溃、不得重复写。

    该分支只在「同名结果行已落库但不在当前 working copy」时命中（例如
    ``include_in_context=False`` 的历史行）。此时无法在不伪造事实的前提下闭合模型输入，
    因此降级为「记 warning + 跳过」：不崩溃、不重复落库，也不凭空构造条目。
    """

    service = _ContractContextService(existing={(1, "call-1")})
    manager = _manager(
        context_service=service,
        task_id=7,
        current_run_id=1,
        entries=[ContextEntry(_ai_message("call-1"), 1, 1)],
        next_sequence=2,
    )

    messages = manager.load_message()

    assert service.appended == []
    assert [type(message) for message in messages] == [SystemMessage, AIMessage]


@pytest.mark.xfail(
    strict=True,
    reason="缺陷: 反向孤儿 ToolMessage（无前置 tool_call）未被处理，模型输入仍是非法协议",
)
def test_orphan_tool_message_is_closed_or_dropped() -> None:
    """期望行为：没有前置 AI 工具调用的 ToolMessage 不得原样进入模型输入。"""

    manager = _manager(
        context_service=_ContractContextService(),
        task_id=7,
        current_run_id=1,
        entries=[
            ContextEntry(ToolMessage(content="stale result", tool_call_id="call-ghost"), 1, 1),
        ],
        next_sequence=2,
    )

    messages = manager.load_message()

    _assert_model_protocol_closed(messages)


@pytest.mark.xfail(
    strict=True,
    reason="缺陷: 占位先占用 (task_id, run_id, tool_call_id)，真实结果随后写入抛 IntegrityError",
)
def test_placeholder_does_not_block_later_real_tool_result(env: SimpleNamespace) -> None:
    """期望行为：占位不得阻塞同一调用真实结果的写入（resume 重放 / 结果迟到）。

    可达路径：run 在工具执行前被取消 -> ``tools_node`` 跳过执行且不写占位 ->
    下一次 ``load_message``（同 run resume 或下一 run）补占位 ->
    该 run 的 pending 调用被重放执行 -> ``ToolCallLifecycleManager.settle`` 经
    ``add_message`` 写真实结果 -> 撞上占位占用的唯一键。
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

    manager.add_message(
        ToolMessage(content="real tool output", tool_call_id="call-1"),
        transport_metadata={"status": "completed"},
    )

    messages = manager.load_message()
    modeled = [message for message in messages if isinstance(message, ToolMessage)]
    assert [message.content for message in modeled] == ["real tool output"]


def test_close_unclosed_tool_call_is_idempotent_and_ordered(env: SimpleNamespace) -> None:
    """正常路径：占位补齐一次、位置正确，二次取数不再新增行。"""

    run = env.seed_run("running")
    first_call = _ai_message("call-1")
    second_call = _ai_message("call-2", "call-3")
    first_result = ToolMessage(content="a done", tool_call_id="call-1")
    third_result = ToolMessage(content="c done", tool_call_id="call-3")
    env.context.append(env.task.id, run.id, first_call, 1, True)
    env.context.append(env.task.id, run.id, first_result, 2, True)
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

    tool_ids = [message.tool_call_id for message in messages if isinstance(message, ToolMessage)]
    assert tool_ids == ["call-1", "call-2", "call-3"]
    _assert_model_protocol_closed(messages)
    assert len([row for row in _rows(env) if isinstance(row.message, ToolMessage)]) == 3

    manager._entries = [
        ContextEntry(message, run.id, index) for index, message in enumerate(messages[1:], start=1)
    ]
    manager.load_message()

    assert len([row for row in _rows(env) if isinstance(row.message, ToolMessage)]) == 3


def test_close_scopes_to_the_last_tool_calling_ai_message(env: SimpleNamespace) -> None:
    """一个 Run 内多步推理：只收口最后一条 AI 消息上未配对的结果，不动更早批次。

    一次模型调用只落库一条 ``AIMessage``；上一批调用在下一次取数前已被 ``settle`` 写全，
    因此「最后一条 AIMessage」就是唯一可能的未闭合位置（``close`` 只从尾部定位）。
    """

    run = env.seed_run("running")
    first_call = _ai_message("call-1")
    first_result = ToolMessage(content="a done", tool_call_id="call-1")
    second_call = _ai_message("call-2", "call-3")
    second_result = ToolMessage(content="b done", tool_call_id="call-2")
    env.context.append(env.task.id, run.id, first_call, 1, True)
    env.context.append(env.task.id, run.id, first_result, 2, True)
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
    assert [message.tool_call_id for message in messages if isinstance(message, ToolMessage)] == [
        "call-1",
        "call-2",
        "call-3",
    ]
    _assert_model_protocol_closed(messages)
    rows = _rows(env)
    assert [type(row.message) for row in rows] == [
        AIMessage,
        ToolMessage,
        AIMessage,
        ToolMessage,
        ToolMessage,
    ]
    placeholders = [
        row
        for row in rows
        if isinstance(row.message, ToolMessage) and row.message.tool_call_id == "call-3"
    ]
    assert len(placeholders) == 1
    assert placeholders[0].run_id == run.id
    assert placeholders[0].transport_metadata == {"status": "cancelled"}
    # 更早批次（call-1）的结果行原样保留，没有被重写或搬动。
    assert next(
        row.sequence for row in rows if isinstance(row.message, ToolMessage)
        and row.message.tool_call_id == "call-1"
    ) == 2


def test_recover_orphaned_runs_cancels_run_and_closes_tool_calls(env: SimpleNamespace) -> None:
    """重启恢复：active Run 收口为取消，并补齐其最后一个 AI 消息上缺失结果的调用。"""

    run = env.seed_run("running")
    ai_message = _ai_message("call-1", "call-2")
    settled = ToolMessage(content="a done", tool_call_id="call-1")
    human = HumanMessage(content="user input")
    env.context.append(env.task.id, run.id, human, 1, True)
    env.context.append(env.task.id, run.id, ai_message, 2, True)
    env.context.append(env.task.id, run.id, settled, 3, True, {"status": "completed"})

    run_crud = _bind(ConversationRunCrud, env.factory)
    service = ConversationRunService.__new__(ConversationRunService)
    service._run = run_crud
    service._context = env.context
    service._session_factory = env.factory

    recovered = service.recover_orphaned_runs()

    assert [record.id for record in recovered] == [run.id]
    # 幂等：Run 已不是 active，重复调用既不返回也不重复写。
    assert service.recover_orphaned_runs() == []
    persisted = run_crud.get(run.id)
    assert persisted.status == "cancelled"
    assert persisted.end_reason == "runtime_restarted"

    rows = _rows(env)
    tool_rows = [row for row in rows if isinstance(row.message, ToolMessage)]
    assert [(row.message.tool_call_id, row.run_id) for row in tool_rows] == [
        ("call-1", run.id),
        ("call-2", run.id),
    ]
    # 只补缺失项：已有结果行原样保留，新增占位带取消终态与调用所属 Run。
    assert tool_rows[0].transport_metadata == {"status": "completed"}
    placeholder = tool_rows[1]
    assert placeholder.transport_metadata == {"status": "cancelled"}
    assert placeholder.run_id == run.id

    # 冷读：Run 终态与工具 part 终态对齐，快照无需额外投影即可通过校验。
    rebuilt = ConversationTaskStateRebuilder.rebuild(env.task, [run], rows)
    validate_snapshot(rebuilt)
    tool_parts = ConversationTaskStateRebuilder.build_pair_tool_part(rows)
    assert {call_id: part["status"] for call_id, part in tool_parts.items()} == {
        "call-1": "completed",
        "call-2": "cancelled",
    }


def test_ensure_run_user_message_writes_once_per_run(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fresh Run 补写一次初始 user 消息；重复调用不再写（幂等）。"""

    projected: list[object] = []
    # 写入后经投影器把 user 消息推给 Transport（进程内投影器依赖存储单例，这里只记录事件）。
    monkeypatch.setattr(
        "app.core.context.runtime_context_manager.get_conversation_event_projector",
        lambda: SimpleNamespace(process=projected.append),
    )
    run = env.seed_run("running")
    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=[],
        next_sequence=1,
    )

    assert manager.ensure_run_user_message("你好") is True
    assert manager.ensure_run_user_message("你好") is False

    users = [row for row in _rows(env) if isinstance(row.message, HumanMessage)]
    assert [(row.run_id, row.message.content) for row in users] == [(run.id, "你好")]
    assert manager.load_message()[-1].content == "你好"
    assert [type(message) for message in manager.load_message()] == [SystemMessage, HumanMessage]
    # 只有真正写入的那一次投影事件（幂等调用不重复投影）。
    assert [type(event).__name__ for event in projected] == ["UserInputAppendedEvent"]


def test_ensure_run_user_message_keeps_existing_message_on_resume(env: SimpleNamespace) -> None:
    """resume：同一 Run 已有 user 消息时不得重复写。"""

    run = env.seed_run("running")
    existing = HumanMessage(content="原始输入")
    env.context.append(env.task.id, run.id, existing, 1, True)
    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=[ContextEntry(existing, run.id, 1)],
        next_sequence=2,
    )

    assert manager.ensure_run_user_message("原始输入") is False
    users = [row for row in _rows(env) if isinstance(row.message, HumanMessage)]
    assert len(users) == 1


def test_ensure_run_user_message_emits_ordered_safe_attachment_parts(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """初始 HumanMessage 写入成功后，事件包含有序附件 part 且不泄漏本机路径。"""

    projected: list[object] = []
    monkeypatch.setattr(
        "app.core.context.runtime_context_manager.get_conversation_event_projector",
        lambda: SimpleNamespace(process=projected.append),
    )
    run = env.seed_run("running")
    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=[],
        next_sequence=1,
    )

    manager.ensure_run_user_message(
        "前 C:/workspace/notes.md 后",
        [f".cosir/Attachment/{'a' * 64}.png"],
        "前 [[cosir-file:file-1]] 后",
        [{
            "id": "file-1",
            "name": "notes.md",
            "content_type": "text/markdown",
            "path": "C:/workspace/notes.md",
        }],
    )

    event = projected[0]
    assert type(event).__name__ == "UserInputAppendedEvent"
    assert event.parts == [
        {"type": "text", "text": "前 ", "status": "completed"},
        {
            "type": "file",
            "file": "cosir-local-file:file-1",
            "name": "notes.md",
            "contentType": "text/markdown",
        },
        {"type": "text", "text": " 后", "status": "completed"},
        {"type": "image", "image": "cosir-attachment://" + "a" * 64},
    ]
    assert "path" not in event.model_dump_json()


def test_ensure_run_user_message_skips_blank_input(env: SimpleNamespace) -> None:
    """空输入 Run：记 warning 并跳过，不落空 user 消息、不抛异常。"""

    run = env.seed_run("running")
    manager = _manager(
        context_service=env.context,
        task_id=env.task.id,
        current_run_id=run.id,
        entries=[],
        next_sequence=1,
    )

    assert manager.ensure_run_user_message("   ") is False
    assert [row for row in _rows(env) if isinstance(row.message, HumanMessage)] == []
