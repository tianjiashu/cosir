"""Task 5 acceptance tests for canonical conversation state and lifecycle boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, update
from sqlalchemy.orm import sessionmaker

from app.assistant_transport.service.conversation_event_projector import (
    ConversationEventProjector,
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
from app.core.events import AssistantTextDeltaEvent
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.models import ConversationRunError, ConversationRunStatus
from app.models.conversation_input import UserInputPresentation
from app.models.conversation_run_progress import ConversationRunProgress
from app.models.enums.conversation_run_status import RunTransition
from app.models.enums.tool_call_status import ToolCallEventStatus
from app.models.message_metadata import ToolMessageMetadata, UserMessageMetadata
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
from app.storage.model.conversation_task_context_model import ConversationTaskContextModel
from app.storage.store_engines import APP_MODELS, initialize_app_schema
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

_USAGE = {
    "input_tokens": 10,
    "output_tokens": 5,
    "total_tokens": 15,
    "cache_hit_tokens": 2,
    "cache_miss_tokens": 8,
    "reasoning_tokens": 1,
}

# 工具执行结果到 Transport 工具 part 的 wire 终态。
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
    monkeypatch.setattr(
        "app.service.conversation_run.task_message_queue.main_session_factory", lambda: factory
    )
    workspaces = _bind(WorkspaceCrud, factory)
    tasks = _bind(TaskCrud, factory)
    runs = _bind(ConversationRunCrud, factory)
    contexts = _bind(ConversationTaskContextCrud, factory)
    context_service = ConversationTaskContextService.__new__(ConversationTaskContextService)
    context_service._crud = contexts
    workspace = workspaces.create("acceptance", str(tmp_path))
    task = tasks.create(workspace.id, "canonical conversation_run")
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
    monkeypatch.setattr(
        "app.service.conversation_run.user_input_service.get_task_crud",
        lambda: tasks,
    )
    monkeypatch.setattr("app.service.depends.get_task_crud", lambda: tasks)
    monkeypatch.setattr("app.service.depends.get_conversation_run_crud", lambda: runs)
    monkeypatch.setattr("app.service.depends.get_conversation_task_context_crud", lambda: contexts)
    monkeypatch.setattr(
        "app.task_runtime.task_runtime_space.get_task_service",
        lambda: SimpleNamespace(get_task=tasks.get),
    )
    monkeypatch.setattr(
        "app.task_runtime.task_runtime_space.get_workspace_service",
        lambda: SimpleNamespace(get_workspace=workspaces.get),
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

    run = store.runs.create(store.task.id, status=status)
    if current:
        store.tasks.set_current_run_id(store.task.id, run.id)
    return run


def _append(store, run_id: int, message: object, *, metadata: object = None) -> None:
    """按 Task 内下一个可用序号追加一条 context 事实。

    序号游标属于调用方（运行期由 ``RuntimeMessageContextManager`` 持有）：本 node_helper 用
    ``max_sequence + 1`` 复现同一口径，避免测试直接依赖 ``(task_id, sequence)`` 唯一键的细节。
    """

    sequence = store.context.max_sequence(store.task.id) + 1
    if metadata is None and message["role"] == "user":
        # 落库的 user 行必须带展示绑定；测试用正文作展示文本。
        metadata = UserMessageMetadata(kind="user_input", presentation=UserInputPresentation(display_text=message["content"])
        )
    store.context.append(store.task.id, run_id, message, sequence, True, metadata)


def _append_user_ai_tool(store, run, call_id: str, result_status: str | None = None) -> None:
    """写入 canonical 用户、assistant 和工具消息，工具元数据使用 Transport 终态口径。
    """

    _append(store, run.id, {"role": "user", "content": f"user-{run.id}"})
    _append(
        store,
        run.id,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "a.py"})},
                }
            ],
            "reasoning_content": None,
        },
    )
    if result_status is None:
        return
    is_success = result_status == "success"
    status_hint = None if is_success else ("已取消" if result_status == "cancelled" else "执行失败")
    _append(
        store,
        run.id,
        {
            "role": "tool",
            "content": "file content" if is_success else "tool failed",
            "tool_call_id": call_id,
        },
        metadata=ToolMessageMetadata(
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
        assert "conversation_commands" not in table_names
        assert "file_snapshots" not in table_names
        backend_app = Path(__file__).parents[1] / "app"
        assert not list(backend_app.rglob("conversation_task_snapshot*.py"))
        forbidden = (
            "conversation_task_snapshots",
            "ConversationTaskSnapshot",
            "ensure_state_snapshot",
        )
        assert not any(
            any(token in path.read_text(encoding="utf-8") for token in forbidden)
            for path in backend_app.rglob("*.py")
        )
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
    first_user = store.contexts.get(store.task.id, include_in_context=False)[0]
    assert [message["id"] for message in state["runs"][0]["messages"]] == [
        first_user.message_id,
        f"assistant-{first_user.message_id}",
    ]
    # 消息身份与正文取自 context，Run.input_text 仅记录首条输入的摘要。
    assert state["runs"][0]["messages"][0]["parts"][0]["text"] == f"user-{completed.id}"
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
        if row.message["role"] == "assistant"
    )
    with store.factory.begin() as session:
        session.execute(
            update(ConversationTaskContextModel)
            .where(ConversationTaskContextModel.id == ai_row.id)
            .values(metadata_json="{not-json")
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
    _append(store, run.id, {"role": "user", "content": "durable user"})
    service = ConversationRunStateService.__new__(ConversationRunStateService)
    service._run = store.runs

    class FailingProjector:
        def process(self, _event: object) -> None:
            raise RuntimeError("SSE failed after commit")

    monkeypatch.setattr(
        "app.service.conversation_run.conversation_run_state_service.service_depends.get_conversation_event_projector",
        lambda: FailingProjector(),
    )
    stats = ConversationRunUsageStats(**_USAGE)
    # 终态落库先于投影：投影失败向上传播，但已提交的 Run 事实与用量必须完好。
    with pytest.raises(RuntimeError, match="SSE failed after commit"):
        service.transition(
            run.id, RunTransition.COMPLETE,
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
    assert state["runs"][0]["messages"][0]["parts"][0]["text"] == "durable user"


@pytest.mark.asyncio
async def test_sse_first_frame_and_projector_are_memory_only_and_disconnect_does_not_cancel(
    canonical_store,
) -> None:
    """SSE registration/read is live-only; disconnect leaves the durable Run untouched."""

    store = canonical_store
    run = _create_run(store, current=True)
    _append(store, run.id, {"role": "user", "content": "user"})
    _append(
        store,
        run.id,
        {"role": "assistant", "content": "canonical", "reasoning_content": None},
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


def test_real_sqlite_fork_preserves_canonical_identity(
    canonical_store,
) -> None:
    """Fork preserves canonical ownership without sharing message/checkpoint identity."""

    store = canonical_store
    source_run = _create_run(store, current=True)
    _append_user_ai_tool(store, source_run, "call-fork", "success")
    _settle_run(store, source_run, "completed", "done")
    store.context.create_system_prompt(
        store.task.id,
        {"role": "system", "content": "fixed system prompt"},
    )
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
    assert target_run.execution_data == ConversationRunProgress()
    source_rows = store.contexts.get(store.task.id, include_in_context=False)
    target_rows = store.contexts.get(target.id, include_in_context=False)
    assert [row.id for row in target_rows] != [row.id for row in source_rows]
    assert [row.message for row in target_rows] == [row.message for row in source_rows]


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
    service._run_state = ConversationRunStateService.__new__(ConversationRunStateService)
    service._run_state._run = store.runs
    recovered = service.recover_orphaned_runs()
    assert [item.id for item in recovered] == [run.id]
    assert service.recover_orphaned_runs() == []
    persisted = store.runs.get(run.id)
    assert persisted.status == "cancelled"
    assert persisted.end_reason == "runtime_restarted"
    repaired = [
        row
        for row in store.contexts.get(store.task.id, include_in_context=False)
        if row.message["role"] == "tool"
    ]
    assert len(repaired) == 1
    assert repaired[0].run_id == run.id
    assert repaired[0].metadata == ToolMessageMetadata.model_validate({"status": "cancelled"})
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
