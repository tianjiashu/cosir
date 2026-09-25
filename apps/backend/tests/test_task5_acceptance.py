"""Task 5 acceptance tests for canonical conversation state and lifecycle boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import inspect, update
from sqlalchemy.orm import sessionmaker

from app.assistant_transport.event import AssistantTextDeltaEvent
from app.assistant_transport.service.conversation_event_projector import (
    ConversationEventProjector,
)
from app.assistant_transport.service.conversation_run_command_service import (
    ConversationRunCommandInput,
    ConversationRunCommandService,
)
from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.service.transport_stream_service import (
    AssistantTransportStreamService,
)
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.models import ConversationRunCommand, ConversationRunError, ConversationRunStatus
from app.models.conversation_task_context import TransportMetadata
from app.models.enums.tool_call_status import ToolCallEventStatus
from app.service.task.conversation_run_service import ConversationRunService
from app.service.task.conversation_run_state_service import ConversationRunStateService
from app.service.task.conversation_task_context_service import ConversationTaskContextService
from app.storage.crud.conversation_command_crud import ConversationCommandCrud
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.engine_cache import create_sqlite_engine
from app.storage.init_schema import APP_MODELS, initialize_app_schema
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


@pytest.fixture(autouse=True)
def _stub_delegation_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """冷读 rebuild 会解析进程级委派 service；本文件不验证委派，注入空替身。

    ``ConversationTaskStateService._rebuild`` 会按 Run 查询该 Run 的委派记录，而解析出的
    委派 service 依赖已初始化主库会话；本文件用独立临时 SQLite（``canonical_store``）装配，
    不经 ``init_storage``，故预置「无委派」替身，保持本文件「不涉及委派」的既有前提。
    """

    class _NoDelegationService:
        """返回空委派集合的最小替身。"""

        def list_by_parent_turn(self, run_id: int) -> list[object]:
            return []

    monkeypatch.setattr(
        "app.service.depends.get_delegation_service", lambda: _NoDelegationService()
    )


_USAGE = {
    "input_tokens": 10,
    "output_tokens": 5,
    "total_tokens": 15,
    "cache_hit_tokens": 2,
    "cache_miss_tokens": 8,
    "reasoning_tokens": 1,
}

# 工具执行结果 -> Transport 工具 part 的 wire 终态（与 tool_call_lifecycle 的映射一致）。
_TOOL_WIRE_STATUS: dict[str, ToolCallEventStatus] = {
    "success": "completed",
    "error": "failed",
    "cancelled": "cancelled",
}


def _bind(crud_type: type[object], factory: sessionmaker) -> object:
    """Bind a real CRUD implementation to the acceptance fixture's SQLite factory."""

    crud = crud_type.__new__(crud_type)
    crud._session_factory = factory  # type: ignore[attr-defined]
    return crud


@pytest.fixture
def canonical_store(tmp_path: Path, monkeypatch):
    """Create a fresh target schema and real CRUD services over a file-backed SQLite database."""

    engine = create_sqlite_engine(tmp_path / "storage" / "app.sqlite3")
    initialize_app_schema(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    workspaces = _bind(WorkspaceCrud, factory)
    tasks = _bind(TaskCrud, factory)
    runs = _bind(ConversationRunCrud, factory)
    contexts = _bind(ConversationTaskContextCrud, factory)
    commands = _bind(ConversationCommandCrud, factory)
    context_service = ConversationTaskContextService.__new__(ConversationTaskContextService)
    context_service._crud = contexts
    workspace = workspaces.create("acceptance", str(tmp_path))
    task = tasks.create(workspace.id, "canonical task")
    # 快照重建按工具名查 display 定义；本文件只验证状态契约，给出最小合法 presentation
    # （``validate_snapshot`` 要求它是对象），不依赖工具系统装配。
    monkeypatch.setattr(
        ConversationTaskStateRebuilder,
        "get_tool_display",
        staticmethod(lambda _name: {"verb": "Read"}),
    )
    ConversationTaskStateService.clear_process_state()
    # ConversationTaskStateService 现在从 depends 取 CRUD 单例；这里把三个 getter 指向本夹具的临时库
    # CRUD，使 state 服务与 store 中的 CRUD 读写同一份临时数据（保留测试隔离，不污染全局库）。
    monkeypatch.setattr("app.service.depends.get_task_crud", lambda: tasks)
    monkeypatch.setattr("app.service.depends.get_conversation_run_crud", lambda: runs)
    monkeypatch.setattr(
        "app.service.depends.get_conversation_task_context_crud", lambda: contexts
    )
    state = ConversationTaskStateService()
    task_runtime_spaces.close()
    store = SimpleNamespace(
        engine=engine,
        factory=factory,
        workspaces=workspaces,
        tasks=tasks,
        runs=runs,
        contexts=contexts,
        commands=commands,
        context=context_service,
        state=state,
        workspace=workspace,
        task=task,
    )
    try:
        yield store
    finally:
        task_runtime_spaces.close()
        ConversationTaskStateService.clear_process_state()
        engine.dispose()


def _create_run(store, *, status: str = "running", current: bool = False):
    """Create a real Run and optionally make it the Task's explicit current Run."""

    run = store.runs.create(store.task.id, f"input for run {status}", status=status)
    if current:
        store.tasks.set_current_run_id(store.task.id, run.id)
    return run


def _append(store, run_id: int, message: object, *, metadata: object = None) -> None:
    """按 Task 内下一个可用序号追加一条 context 事实。

    序号游标属于调用方（运行期由 ``RuntimeContextManager`` 持有）：本 node_helper 用
    ``max_sequence + 1`` 复现同一口径，避免测试直接依赖 ``(task_id, sequence)`` 唯一键的细节。
    """

    sequence = store.context.max_sequence(store.task.id) + 1
    store.context.append(store.task.id, run_id, message, sequence, True, metadata)


def _append_user_ai_tool(store, run, call_id: str, result_status: str | None = None) -> None:
    """Persist canonical user, AI/tool-call, and optional ToolMessage facts.

    AI 消息与运行期同形：``content=""`` + ``additional_kwargs["reasoning_content"]=None``
    （快照重建按这两个通道取文本/reasoning part）。工具结果行随行写入 wire 口径的
    ``TransportMetadata``（``status`` 取终态、``display_data`` 与短提示分列），与运行期
    ``ToolCallLifecycleManager.settle`` 的写入口径一致。
    """

    _append(store, run.id, HumanMessage(content=f"user-{run.id}"))
    _append(
        store,
        run.id,
        AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": call_id}],
            additional_kwargs={"reasoning_content": None},
        ),
    )
    if result_status is None:
        return
    is_success = result_status == "success"
    status_hint = (
        None if is_success else ("已取消" if result_status == "cancelled" else "执行失败")
    )
    _append(
        store,
        run.id,
        ToolMessage(
            content="file content" if is_success else "tool failed",
            tool_call_id=call_id,
            status="success" if is_success else "error",
        ),
        metadata=TransportMetadata(
            status=_TOOL_WIRE_STATUS[result_status],
            display_data=(
                {"kind": "read-file-meta", "path": "a.py"}
                if is_success
                else {"status_hint": status_hint}
            ),
            error=status_hint,
        ),
    )


def _settle_run(store, run, status: str, end_reason: str) -> None:
    """Persist terminal Run facts through the real Run CRUD conditional update."""

    error: ConversationRunError | None = (
        None if status == "completed" else {"code": end_reason, "message": "运行失败"}
    )
    updated = store.runs.update_status_if_in(
        run.id,
        status,
        (ConversationRunStatus.RUNNING.value,),
        end_reason=end_reason,
        final_output="run summary must not become a UI message",
        usage=_USAGE,
        error=error,
    )
    assert updated is not None


def test_fresh_target_schema_has_no_persisted_conversation_snapshot_surface(tmp_path: Path) -> None:
    """A fresh target SQLite schema exposes only canonical conversation fact tables."""

    engine = create_sqlite_engine(tmp_path / "fresh.sqlite3")
    try:
        initialize_app_schema(engine)
        table_names = set(inspect(engine).get_table_names())
        assert "conversation_task_snapshots" not in table_names
        assert {model.__tablename__ for model in APP_MODELS}.isdisjoint(
            {"conversation_task_snapshots"}
        )
        assert "conversation_task_contexts" in table_names
        assert "file_snapshots" not in table_names
        backend_app = Path(__file__).parents[1] / "app"
        assert not list(backend_app.rglob("conversation_task_snapshot*.py"))
        forbidden = (
            "conversation_task_snapshots",
            "ConversationTaskSnapshot",
            "state_json",
            "ensure_state_snapshot",
        )
        assert not any(
            any(token in path.read_text(encoding="utf-8") for token in forbidden)
            for path in backend_app.rglob("*.py")
        )
    finally:
        engine.dispose()


def test_schema_removes_legacy_file_snapshot_table(tmp_path: Path) -> None:
    """Existing local databases no longer retain the removed file changes table."""

    engine = create_sqlite_engine(tmp_path / "legacy.sqlite3")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE file_snapshots (id INTEGER PRIMARY KEY, task_id INTEGER)"
            )
        initialize_app_schema(engine)
        assert "file_snapshots" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_real_sqlite_cold_state_and_agent_context_rebuild_all_canonical_tool_outcomes(
    canonical_store,
) -> None:
    """Cold Transport and Agent context use real Task/Run/Context rows for all tool outcomes."""

    store = canonical_store
    completed = _create_run(store, current=True)
    failed = _create_run(store)
    cancelled = _create_run(store, current=True)
    _append_user_ai_tool(store, completed, "call-success", "success")
    _append_user_ai_tool(store, failed, "call-failed", "error")
    _append_user_ai_tool(store, cancelled, "call-cancelled", "cancelled")
    _settle_run(store, completed, "completed", "done")
    _settle_run(store, failed, "failed", "runtime_failed")
    _settle_run(store, cancelled, "cancelled", "user_cancelled")
    store.tasks.set_current_run_id(store.task.id, cancelled.id)

    state = store.state.get_state(store.task.id)
    assert [run["runId"] for run in state["runs"]] == [completed.id, failed.id, cancelled.id]
    assert state["current_run_id"] == cancelled.id
    assert [message["id"] for message in state["runs"][0]["messages"]] == [
        f"user-{completed.id}",
        f"assistant-{completed.id}",
    ]
    # 冷读的 user 文本以 Run 输入事实为准（``build_user_message`` 取 extra.display_text
    # 或 run.input_text），context 里的 HumanMessage 只用于判定「该 Run 有用户消息」。
    assert state["runs"][0]["messages"][0]["parts"][0]["text"] == completed.input_text
    # 工具终态由 Run 分组配对产出（同一份 canonical 行同时驱动冷读快照与模型上下文）。
    tool_parts = ConversationTaskStateRebuilder.build_pair_tool_part(
        store.contexts.get(store.task.id, include_in_context=False)
    )
    assert {call_id: part["status"] for call_id, part in tool_parts.items()} == {
        "call-success": "completed",
        "call-failed": "failed",
        "call-cancelled": "cancelled",
    }
    assert all(
        "run summary must not become a UI message" not in str(message)
        for run in state["runs"]
        for message in run["messages"]
    )


def test_real_sqlite_malformed_metadata_fails_and_not_as_empty_state(
    canonical_store,
) -> None:
    """Malformed durable metadata is an explicit cold-read failure."""

    store = canonical_store
    run = _create_run(store, current=True)
    _append_user_ai_tool(store, run, "call-malformed")
    ai_row = next(
        row
        for row in store.contexts.get(store.task.id, include_in_context=False)
        if isinstance(row.message, AIMessage)
    )
    with store.factory.begin() as session:
        session.execute(
            update(ConversationTaskContextModel)
            .where(ConversationTaskContextModel.id == ai_row.id)
            .values(transport_metadata_json="{not-json")
        )

    # ``ConversationTaskContextRecord._from_model`` 用原生 ``json.loads`` 反序列化 metadata，
    # 损坏的 JSON 以 ``json.JSONDecodeError``（``ValueError`` 子类）向冷读边界传播。
    with pytest.raises(ValueError):
        store.state.get_state(store.task.id)


def test_post_commit_projector_failure_leaves_real_run_facts_and_cold_read_durable(
    canonical_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport failure after commit cannot erase terminal Run facts."""

    store = canonical_store
    run = _create_run(store, current=True)
    _append(store, run.id, HumanMessage(content="durable user"))
    service = ConversationRunStateService.__new__(ConversationRunStateService)
    service._run = store.runs

    class FailingProjector:
        def process(self, _event: object) -> None:
            raise RuntimeError("SSE failed after commit")

    monkeypatch.setattr(
        "app.service.task.conversation_run_state_service.service_depends.get_conversation_event_projector",
        lambda: FailingProjector(),
    )
    stats = ConversationRunUsageStats(**_USAGE)
    # 终态落库先于投影：投影失败向上传播，但已提交的 Run 事实与用量必须完好。
    with pytest.raises(RuntimeError, match="SSE failed after commit"):
        service.complete_run_if_running(
            run.id,
            final_output="durable final summary",
            usage_stats=stats,
        )
    persisted = store.runs.get(run.id)
    assert persisted.status == "completed"
    assert persisted.final_output == "durable final summary"
    assert persisted.usage == _USAGE
    ConversationTaskStateService.clear_process_state()
    state = store.state.get_state(store.task.id)
    assert state["runs"][0]["status"] == "completed"
    # 同上：冷读 user 文本取 Run 输入事实，而非 context 行正文。
    assert state["runs"][0]["messages"][0]["parts"][0]["text"] == run.input_text


@pytest.mark.asyncio
async def test_sse_first_frame_and_projector_are_memory_only_and_disconnect_does_not_cancel(
    canonical_store,
) -> None:
    """SSE registration/read is live-only; disconnect leaves the durable Run untouched."""

    store = canonical_store
    run = _create_run(store, current=True)
    _append(store, run.id, HumanMessage(content="user"))
    _append(
        store,
        run.id,
        AIMessage(content="canonical", additional_kwargs={"reasoning_content": None}),
    )
    stream_service = AssistantTransportStreamService.__new__(AssistantTransportStreamService)
    stream_service._snapshots = store.state
    stream = stream_service.stream(store.task.id, run.id, lambda: False)
    first = await anext(stream)
    assert first.state["runs"][0]["messages"][1]["parts"][0]["text"] == "canonical"
    before_count = len(store.contexts.get(store.task.id, include_in_context=False))
    await stream.aclose()
    assert store.runs.get(run.id).status == "running"
    projector = ConversationEventProjector(state_service=store.state)
    projector.process(
        AssistantTextDeltaEvent(
            event_id="live-delta",
            task_id=store.task.id,
            run_id=run.id,
            part="text",
            delta=" live",
        )
    )
    assert len(store.contexts.get(store.task.id, include_in_context=False)) == before_count


def _command_service(store) -> ConversationRunCommandService:
    """Assemble the command service with real CRUD and no process-wide storage singleton."""

    run_service = ConversationRunService.__new__(ConversationRunService)
    run_service._task = store.tasks
    run_service._run = store.runs
    run_service._context = store.context
    run_service._session_factory = store.factory
    run_state_service = ConversationRunStateService.__new__(ConversationRunStateService)
    run_state_service._run = store.runs
    run_state_service._session_factory = store.factory
    service = ConversationRunCommandService.__new__(ConversationRunCommandService)
    service._command = store.commands
    service._conversation_run = run_service
    service._run_state = run_state_service
    service._state = store.state
    service._context = store.context
    service._task = SimpleNamespace()
    return service


@pytest.mark.asyncio
async def test_real_sqlite_same_task_is_mutually_exclusive_and_same_command_is_idempotent(
    canonical_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real command writes serialize per Task while duplicate commands create one Run."""

    store = canonical_store
    service = _command_service(store)
    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_run_command_service.main_session_factory",
        lambda: store.factory,
    )
    task_runtime_spaces.close()
    results = await asyncio.gather(
        *(
            asyncio.to_thread(
                service.start_or_attach,
                commands=[
                    ConversationRunCommandInput(command_id="same-command", command_type="new")
                ],
                payload_hash="same-payload",
                provider_id=None,
                model_name=None,
                reasoning_effort=None,
                task_id=store.task.id,
                run_command=ConversationRunCommand(display_text="hello"),
            )
            for _ in range(2)
        )
    )
    assert sorted(result.created for result in results) == [False, True]
    assert len(store.runs.list_by_task(store.task.id)) == 1
    # 幂等只体现在 Run 行：user 消息由 workflow 在 graph 启动前写入，命令服务不写 context。
    assert store.contexts.get(store.task.id, include_in_context=False) == []
    first_run = store.runs.list_by_task(store.task.id)[0]
    # 第一个 Run 必须经状态 service 收口，而不是直接改 CRUD：Run 终态要靠事件投影同步到
    # 快照，否则快照里上一个 Run 仍是 running，后续 Run 的初始化投影会被 ``RunInitializedEvent``
    # 的「仅在上一个 Run 已终态时才追加」规则合法拒绝，进而令状态事件找不到目标 Run。
    completed = service._run_state.complete_run_if_running(
        first_run.id,
        final_output="test_complete",
        usage_stats=ConversationRunUsageStats(**_USAGE),
    )
    assert completed is not None
    assert completed.status == ConversationRunStatus.COMPLETED.value

    second_task = store.tasks.create(store.workspace.id, "second task")
    # 不同 task 之间本无互斥/幂等关系，故这两次调用串行执行：本用例要验证的「同 task 互斥 +
    # 同 command 幂等」已由上方并发段落覆盖，而进程级 TaskRuntimeSpace 注册表与文件级 SQLite
    # 在「多个 task 的首次 Run 创建」同时进入冷读装载时会互相争用，串行可让断言只反映语义。
    other_results = [
        await asyncio.to_thread(
            service.start_or_attach,
            commands=[
                ConversationRunCommandInput(command_id="task-one-command", command_type="new")
            ],
            payload_hash="payload-one",
            provider_id=None,
            model_name=None,
            reasoning_effort=None,
            task_id=store.task.id,
            run_command=ConversationRunCommand(display_text="one"),
        ),
        await asyncio.to_thread(
            service.start_or_attach,
            commands=[
                ConversationRunCommandInput(command_id="task-two-command", command_type="new")
            ],
            payload_hash="payload-two",
            provider_id=None,
            model_name=None,
            reasoning_effort=None,
            task_id=second_task.id,
            run_command=ConversationRunCommand(display_text="two"),
        ),
    ]
    assert all(result.created for result in other_results)
    assert len(store.runs.list_by_task(store.task.id)) == 2
    assert len(store.runs.list_by_task(second_task.id)) == 1


def test_real_sqlite_fork_and_edit_preserve_canonical_identity(
    canonical_store,
) -> None:
    """Fork and edit preserve canonical ownership without sharing message/checkpoint identity."""

    store = canonical_store
    source_run = _create_run(store, current=True)
    _append_user_ai_tool(store, source_run, "call-fork", "success")
    _settle_run(store, source_run, "completed", "done")
    target = store.tasks.create(store.workspace.id, "fork")
    with store.factory.begin() as session:
        target_run = store.runs.clone_for_task(session, source_run, target.id)
        store.context.clone_for_fork(
            store.task.id,
            target.id,
            {source_run.id: target_run.id},
            session,
        )
    assert target.current_run_id is None
    assert target_run.checkpoint_thread_id != source_run.checkpoint_thread_id
    source_rows = store.contexts.get(store.task.id, include_in_context=False)
    target_rows = store.contexts.get(target.id, include_in_context=False)
    assert [row.id for row in target_rows] != [row.id for row in source_rows]
    assert [row.message for row in target_rows] == [row.message for row in source_rows]

    new_checkpoint = "edit-checkpoint"
    with store.factory.begin() as session:
        reset = store.runs.reset_for_edit(
            source_run.id,
            "edited input",
            new_checkpoint,
            (ConversationRunStatus.COMPLETED.value,),
            session=session,
        )
        assert reset is not None
        store.context.delete_by_run_id(store.task.id, source_run.id, session=session)
        store.context.append(
            store.task.id,
            source_run.id,
            HumanMessage(content="edited input"),
            1,
            True,
            None,
            session=session,
        )
    edited = store.runs.get(source_run.id)
    assert edited.status == "pending"
    assert edited.final_output is None
    assert edited.usage is None
    assert edited.checkpoint_thread_id == new_checkpoint
    rows_after_edit = store.contexts.get(store.task.id, include_in_context=False)
    assert len(rows_after_edit) == 1
    assert rows_after_edit[0].message.content == "edited input"


def test_real_sqlite_restart_recovery_is_bounded_repairs_tools_and_never_replays(
    canonical_store,
) -> None:
    """Restart recovery commits cancelled Run/tool facts once and does not invoke workflow."""

    store = canonical_store
    run = _create_run(store, current=True)
    _append_user_ai_tool(store, run, "call-interrupted")
    service = ConversationRunService.__new__(ConversationRunService)
    service._run = store.runs
    service._context = store.context
    service._session_factory = store.factory
    recovered = service.recover_orphaned_runs()
    assert [item.id for item in recovered] == [run.id]
    assert service.recover_orphaned_runs() == []
    persisted = store.runs.get(run.id)
    assert persisted.status == "cancelled"
    assert persisted.end_reason == "runtime_restarted"
    repaired = [
        row
        for row in store.contexts.get(store.task.id, include_in_context=False)
        if isinstance(row.message, ToolMessage)
    ]
    assert len(repaired) == 1
    assert repaired[0].run_id == run.id
    assert repaired[0].transport_metadata == {"status": "cancelled"}
    ConversationTaskStateService.clear_process_state()
    state = store.state.get_state(store.task.id)
    assert state["runs"][0]["status"] == "cancelled"
    # 工具终态由 Run 分组配对产出（占位行写入 wire 口径的 cancelled）。
    repaired_parts = ConversationTaskStateRebuilder.build_pair_tool_part(
        store.contexts.get(store.task.id, include_in_context=False)
    )
    assert {call_id: part["status"] for call_id, part in repaired_parts.items()} == {
        "call-interrupted": "cancelled"
    }



