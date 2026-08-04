"""Turn 回退功能测试（pytest）。

测试分层：
- T1/T3 纯函数：v4a_reverse 反向映射、build_forward_operations。
- T2 CRUD：file_snapshot_crud 往返（临时库）。
- T4 文件还原：apply_all_with_diff 反向操作磁盘还原（tmp_path）。
- T5/T6 编排：revert_turn 两段式 + D6 守卫 + 幂等。
- T8 delete before 读取 + 调度器采集落库。

测试隔离：每个用例用 tmp_path 作为 workspace 根，Settings.override 指向独立 DB 与
checkpoint 文件，init_storage 建表，teardown 调 close_storage + reset_service_dependencies。
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config.settings import Settings
from app.models.enums.turn_status import TurnStatus
from app.models.file_snapshot_record import FileSnapshotRecord
from app.service.depends import reset_service_dependencies
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
from app.storage.crud.runtime_event_crud import RuntimeEventCrud
from app.storage.crud.turn_crud import TurnCrud
from app.storage.store_engines import (
    close_storage,
    init_storage,
    main_session_factory,
)
from app.tools.tool_handler.patch.patch_apply import apply_all_with_diff
from app.tools.tool_handler.patch.patch_parser import (
    Hunk,
    HunkLine,
    OperationType,
    PatchOperation,
)
from app.tools.tool_handler.patch.v4a_reverse import (
    build_forward_operations,
    reverse_v4a_operation,
)
from app.tools.tool_handler.security.project_path import ProjectPathResolver


@pytest.fixture
def isolated_storage(tmp_path: Path):
    """为每个测试搭建独立的 storage（主库 + checkpoint + 日志库）。"""
    Settings.override(
        DATABASE_FILE=tmp_path / "app.sqlite3",
        CHECKPOINT_FILE=tmp_path / "ckpt.sqlite",
        LOG_DATABASE_FILE=tmp_path / "logs.sqlite3",
    )
    init_storage()
    reset_service_dependencies()
    yield {"tmp_path": tmp_path}
    close_storage()
    reset_service_dependencies()


def _seed_task_turn(workspace_root: Path, task_id: str, turn_id: str, status: str) -> None:
    """直接插 workspace/task/turn 基础行（绕过繁琐的 service 初始化）。"""
    with main_session_factory()() as session:
        session.execute(
            text(
                "INSERT OR IGNORE INTO workspaces "
                "(workspace_id, name, root_path, created_at, updated_at) "
                "VALUES ('ws1', 'ws', :root, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            ),
            {"root": str(workspace_root)},
        )
        session.execute(
            text(
                "INSERT OR IGNORE INTO tasks "
                "(task_id, workspace_id, agent_id, input_text, title, "
                "last_message_preview, status, created_at, updated_at) "
                "VALUES (:tid, 'ws1', 'a1', 'x', 't', '', 'pending', "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            ),
            {"tid": task_id},
        )
        session.execute(
            text(
                "INSERT OR IGNORE INTO turns "
                "(turn_id, task_id, input_text, status, created_at, updated_at) "
                "VALUES (:turn_id, :tid, 'x', :status, '2026-01-01T00:00:01Z', "
                "'2026-01-01T00:00:01Z')"
            ),
            {"turn_id": turn_id, "tid": task_id, "status": status},
        )
        session.commit()


def _save_snapshot(turn_id: str, op_json: dict, path: str = "a.txt", action: str = "add") -> None:
    FileSnapshotCrud().save(
        FileSnapshotRecord(
            turn_id=turn_id,
            seq=0,
            tool_name="write_file",
            tool_call_id="c1",
            path=path,
            action=action,
            op_json=json.dumps(op_json, ensure_ascii=False),
        )
    )


def _write(workspace_root: Path, rel: str, content: str) -> None:
    p = workspace_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# T1 / T3 纯函数：v4a_reverse 四类映射 + build_forward_operations
# ---------------------------------------------------------------------------


def test_reverse_add_becomes_delete():
    forward = PatchOperation(
        operation=OperationType.ADD,
        file_path="a.txt",
        hunks=[Hunk(lines=[HunkLine(prefix="+", content="hello")])],
    )
    rev = reverse_v4a_operation(forward)
    assert rev.operation == OperationType.DELETE
    assert rev.file_path == "a.txt"


def test_reverse_delete_becomes_add_with_content():
    forward = PatchOperation(
        operation=OperationType.DELETE,
        file_path="a.txt",
        hunks=[Hunk(lines=[HunkLine(prefix="+", content="original content")])],
    )
    rev = reverse_v4a_operation(forward)
    assert rev.operation == OperationType.ADD
    assert "original content" in rev.hunks[0].lines[0].content


def test_reverse_update_swaps_hunks():
    forward = PatchOperation(
        operation=OperationType.UPDATE,
        file_path="a.txt",
        hunks=[
            Hunk(
                lines=[
                    HunkLine(prefix=" ", content="line1"),
                    HunkLine(prefix="-", content="old"),
                    HunkLine(prefix="+", content="new"),
                ]
            )
        ],
    )
    rev = reverse_v4a_operation(forward)
    assert rev.operation == OperationType.UPDATE
    prefixes = [(ln.prefix, ln.content) for ln in rev.hunks[0].lines]
    assert ("-", "new") in prefixes
    assert ("+", "old") in prefixes


def test_reverse_move_swaps_paths():
    forward = PatchOperation(
        operation=OperationType.MOVE,
        file_path="src.txt",
        new_path="dst.txt",
    )
    rev = reverse_v4a_operation(forward)
    assert rev.operation == OperationType.MOVE
    assert rev.file_path == "dst.txt"
    assert rev.new_path == "src.txt"


def test_build_forward_operations_from_changes():
    changes = [
        {"path": "new.txt", "new_path": None, "status": "added", "before": "", "after": "x"},
        {"path": "mod.txt", "new_path": None, "status": "modified", "before": "a", "after": "b"},
        {"path": "gone.txt", "new_path": None, "status": "deleted", "before": "y", "after": ""},
        {"path": "s.txt", "new_path": "d.txt", "status": "moved", "before": "z", "after": "z"},
    ]
    ops = build_forward_operations(changes)
    assert ops[0].operation == OperationType.ADD
    assert ops[1].operation == OperationType.UPDATE
    assert ops[2].operation == OperationType.DELETE
    assert ops[3].operation == OperationType.MOVE


# ---------------------------------------------------------------------------
# T2 CRUD：file_snapshot_crud 往返
# ---------------------------------------------------------------------------


def test_file_snapshot_crud_roundtrip(isolated_storage):
    crud = FileSnapshotCrud()
    _save_snapshot(
        "t1", {"operation": "delete", "file_path": "a.txt", "new_path": None, "hunks": []}
    )
    rows = crud.list_by_turn("t1")
    assert len(rows) == 1
    assert rows[0].turn_id == "t1"
    PatchOperation(**json.loads(rows[0].op_json))
    crud.clear_by_turn("t1")
    assert crud.list_by_turn("t1") == []


def test_file_snapshot_crud_chinese_path(isolated_storage):
    crud = FileSnapshotCrud()
    _save_snapshot(
        "t1",
        {"operation": "delete", "file_path": "中文目录/文件.txt", "new_path": None, "hunks": []},
        path="中文目录/文件.txt",
    )
    rows = crud.list_by_turn("t1")
    assert "中文目录/文件.txt" in rows[0].op_json


# ---------------------------------------------------------------------------
# T4 文件还原端到端（tmp_path + apply_all_with_diff）
# ---------------------------------------------------------------------------


def test_restore_added_file_removed(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write(workspace, "new.txt", "created")
    forward = PatchOperation(
        operation=OperationType.ADD,
        file_path="new.txt",
        hunks=[Hunk(lines=[HunkLine(prefix="+", content="created")])],
    )
    apply_all_with_diff([reverse_v4a_operation(forward)], ProjectPathResolver(workspace))
    assert not (workspace / "new.txt").exists()


def test_restore_deleted_file_recreated(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # 回退场景：文件已被 turn 删除（此处不预写，模拟"已不存在"状态），
    # 反向 DELETE→ADD 应重建文件并写入 before 全文。
    forward = PatchOperation(
        operation=OperationType.DELETE,
        file_path="gone.txt",
        hunks=[Hunk(lines=[HunkLine(prefix="+", content="original")])],
    )
    apply_all_with_diff([reverse_v4a_operation(forward)], ProjectPathResolver(workspace))
    assert (workspace / "gone.txt").read_text(encoding="utf-8") == "original"


def test_restore_modified_file_before(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # turn 把 before 改成 after（文件现处于 after 状态）：与反向 hunk 上下文匹配
    _write(workspace, "mod.txt", "ctx\nafter")
    forward = PatchOperation(
        operation=OperationType.UPDATE,
        file_path="mod.txt",
        hunks=[
            Hunk(
                lines=[
                    HunkLine(prefix=" ", content="ctx"),
                    HunkLine(prefix="-", content="before"),
                    HunkLine(prefix="+", content="after"),
                ]
            )
        ],
    )
    # 反向：把 after 改回 before
    apply_all_with_diff([reverse_v4a_operation(forward)], ProjectPathResolver(workspace))
    assert (workspace / "mod.txt").read_text(encoding="utf-8") == "ctx\nbefore"


def test_restore_crlf_preserved(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    p = workspace / "crlf.txt"
    # turn 已把 old 改成 new（CRLF 行尾）。文件现处于 new 状态，回退应还原 old 且保留 CRLF。
    p.write_bytes(b"line1\r\nnew\r\n")
    forward = PatchOperation(
        operation=OperationType.UPDATE,
        file_path="crlf.txt",
        hunks=[
            Hunk(
                lines=[
                    HunkLine(prefix=" ", content="line1"),
                    HunkLine(prefix="-", content="old"),
                    HunkLine(prefix="+", content="new"),
                ]
            )
        ],
    )
    apply_all_with_diff([reverse_v4a_operation(forward)], ProjectPathResolver(workspace))
    assert p.read_bytes() == b"line1\r\nold\r\n"


# ---------------------------------------------------------------------------
# T5 / T6 编排：revert_turn（两段式 + D6 守卫 + 幂等）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revert_turn_restores_and_clears(isolated_storage):
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _write(ws, "created.txt", "hello")
    _seed_task_turn(ws, "task1", "turn1", TurnStatus.COMPLETED.value)
    _save_snapshot(
        "turn1",
        {"operation": "delete", "file_path": "created.txt", "new_path": None, "hunks": []},
        path="created.txt",
    )
    from app.service.task.turn_revert_service import revert_turn

    result = await revert_turn("turn1", workspace_root=ws)
    assert result.ok
    assert result.file_reverted
    assert not (ws / "created.txt").exists()
    assert TurnCrud().get("turn1").status == TurnStatus.REVERTED.value
    assert FileSnapshotCrud().list_by_turn("turn1") == []
    events = RuntimeEventCrud().list_by_turn("turn1")
    assert any(e["event_type"] == "turn_reverted" for e in events)


@pytest.mark.asyncio
async def test_revert_turn_d6_guard_rejects_non_latest(isolated_storage):
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1", TurnStatus.COMPLETED.value)
    _seed_task_turn(ws, "task1", "turn2", TurnStatus.COMPLETED.value)
    from app.service.task.turn_revert_service import revert_turn

    with pytest.raises(ValueError, match="only the latest finished turn can be reverted"):
        await revert_turn("turn1", workspace_root=ws)


@pytest.mark.asyncio
async def test_revert_turn_rejects_running(isolated_storage):
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1", TurnStatus.RUNNING.value)
    from app.service.task.turn_revert_service import revert_turn

    with pytest.raises(ValueError, match="turn not finished"):
        await revert_turn("turn1", workspace_root=ws)


@pytest.mark.asyncio
async def test_revert_turn_idempotent(isolated_storage):
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1", TurnStatus.COMPLETED.value)
    from app.service.task.turn_revert_service import revert_turn

    r1 = await revert_turn("turn1", workspace_root=ws)
    r2 = await revert_turn("turn1", workspace_root=ws)
    assert r1.ok and r2.ok
    assert TurnCrud().get("turn1").status == TurnStatus.REVERTED.value


# ---------------------------------------------------------------------------
# T8 delete before 读取 + 调度器采集
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_records_snapshot_with_before(isolated_storage):
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.delete import build_delete_definition
    from app.tools.tool_registry import ToolRegistry

    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _write(ws, "del.txt", "must-survive")
    _seed_task_turn(ws, "task1", "turn_del", TurnStatus.COMPLETED.value)

    registry = ToolRegistry()
    registry.register(build_delete_definition())
    scheduler = ToolScheduler(registry)
    ctx = ToolExecutionContext(
        task_id="task1", workspace_id="ws1", workspace_root=ws, turn_id="turn_del"
    )
    obs = scheduler.execute(
        ToolCall(tool_name="delete", arguments={"path": "del.txt"}, call_id="cd1"),
        execution_context=ctx,
    )
    assert obs.status == "success"
    assert "changes" in obs.display_data
    snaps = FileSnapshotCrud().list_by_turn("turn_del")
    assert len(snaps) == 1
    assert "must-survive" in snaps[0].op_json


# ---------------------------------------------------------------------------
# 审查补充：真实多操作 / 采集链路 / 相对路径 端到端覆盖
# ---------------------------------------------------------------------------


def test_build_forward_modified_hunk_applies(tmp_path: Path):
    """B2 回归：经 build_forward_operations 产出的 modified 正向 hunk 必须能被 apply。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write(workspace, "f.txt", "before\nline\n")
    changes = [
        {
            "path": "f.txt",
            "new_path": None,
            "status": "modified",
            "before": "before\nline\n",
            "after": "after\nline\n",
        }
    ]
    forward = build_forward_operations(changes)[0]
    # 正向：把 before 改成 after
    apply_all_with_diff([forward], ProjectPathResolver(workspace))
    assert (workspace / "f.txt").read_text(encoding="utf-8") == "after\nline\n"
    # 反向：把 after 改回 before
    apply_all_with_diff([reverse_v4a_operation(forward)], ProjectPathResolver(workspace))
    assert (workspace / "f.txt").read_text(encoding="utf-8") == "before\nline\n"


@pytest.mark.asyncio
async def test_revert_multiple_ops_same_file_ordered(isolated_storage):
    """B1 回归：同 turn 多次操作（写→改→删）逆序回退后磁盘 == 初始。"""
    workspace = isolated_storage["tmp_path"] / "ws"
    workspace.mkdir()
    _write(workspace, "f.txt", "initial")
    # 模拟 turn 执行后的真实终态：写→改→删，磁盘上 f.txt 已不存在
    (workspace / "f.txt").unlink()
    from app.service.task.turn_revert_service import revert_turn

    _seed_task_turn(workspace, "task1", "t1", TurnStatus.COMPLETED.value)

    # 模拟采集：写(ADD)→改(UPDATE)→删(DELETE)，seq 递增
    FSR = FileSnapshotRecord
    crud = FileSnapshotCrud()
    # 写：反向 DELETE
    crud.save(
        FSR(
            turn_id="t1",
            seq=0,
            tool_name="write_file",
            tool_call_id="c0",
            path="f.txt",
            action="add",
            op_json=json.dumps(
                {"operation": "delete", "file_path": "f.txt", "new_path": None, "hunks": []}
            ),
        )
    )
    # 改：反向 UPDATE（changed→initial，即删 changed 加 initial）
    crud.save(
        FSR(
            turn_id="t1",
            seq=1,
            tool_name="patch",
            tool_call_id="c1",
            path="f.txt",
            action="update",
            op_json=json.dumps(
                {
                    "operation": "update",
                    "file_path": "f.txt",
                    "new_path": None,
                    "hunks": [
                        {
                            "lines": [
                                {"prefix": "-", "content": "changed"},
                                {"prefix": "+", "content": "initial"},
                            ]
                        }
                    ],
                }
            ),
        )
    )
    # 删：反向 ADD（重建 initial）
    crud.save(
        FSR(
            turn_id="t1",
            seq=2,
            tool_name="delete",
            tool_call_id="c2",
            path="f.txt",
            action="delete",
            op_json=json.dumps(
                {
                    "operation": "add",
                    "file_path": "f.txt",
                    "new_path": None,
                    "hunks": [
                        {
                            "lines": [
                                {"prefix": "+", "content": "changed"},
                            ]
                        }
                    ],
                }
            ),
        )
    )

    result = await revert_turn("t1", workspace_root=workspace)
    assert result.ok
    # 逆序：先删(ADD 重建 changed)→改(UPDATE changed→initial)→写(DELETE 删 initial)
    assert not (workspace / "f.txt").exists()
    assert TurnCrud().get("t1").status == TurnStatus.REVERTED.value


@pytest.mark.asyncio
async def test_delete_then_revert_recreates_file(isolated_storage):
    """B3 回归：delete 采集相对路径，完整 revert_turn 重建文件内容。"""
    from app.service.task.turn_revert_service import revert_turn
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.delete import build_delete_definition
    from app.tools.tool_registry import ToolRegistry

    workspace = isolated_storage["tmp_path"] / "ws"
    workspace.mkdir()
    _write(workspace, "del.txt", "recover-me")
    _seed_task_turn(workspace, "task1", "turn_del", TurnStatus.COMPLETED.value)

    registry = ToolRegistry()
    registry.register(build_delete_definition())
    scheduler = ToolScheduler(registry)
    ctx = ToolExecutionContext(
        task_id="task1", workspace_id="ws1", workspace_root=workspace, turn_id="turn_del"
    )
    obs = scheduler.execute(
        ToolCall(tool_name="delete", arguments={"path": "del.txt"}, call_id="cd1"),
        execution_context=ctx,
    )
    assert obs.status == "success"
    # 文件已被删除
    assert not (workspace / "del.txt").exists()
    # 完整回退：重建文件且内容恢复
    result = await revert_turn("turn_del", workspace_root=workspace)
    assert result.ok
    assert (workspace / "del.txt").read_text(encoding="utf-8") == "recover-me"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "no-trailing-newline",  # 无尾换行
        "with-trailing-newline\n",  # 单尾换行
        "a\nb\n",  # 多行 + 尾换行
        "a\nb",  # 多行无尾换行
        "crlf\r\nstyle\r\n",  # CRLF 行尾
    ],
)
async def test_delete_revert_preserves_exact_bytes(isolated_storage, content: str):
    """BL-1 回归：delete→revert 后文件字节与原始完全一致（含尾换行 / CRLF / BOM）。"""
    from app.service.task.turn_revert_service import revert_turn
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.delete import build_delete_definition
    from app.tools.tool_registry import ToolRegistry

    workspace = isolated_storage["tmp_path"] / "ws"
    workspace.mkdir()
    target = workspace / "exact.txt"
    target.write_text(content, encoding="utf-8")
    original_bytes = target.read_bytes()
    _seed_task_turn(workspace, "task1", "turn_x", TurnStatus.COMPLETED.value)

    registry = ToolRegistry()
    registry.register(build_delete_definition())
    scheduler = ToolScheduler(registry)
    ctx = ToolExecutionContext(
        task_id="task1", workspace_id="ws1", workspace_root=workspace, turn_id="turn_x"
    )
    obs = scheduler.execute(
        ToolCall(tool_name="delete", arguments={"path": "exact.txt"}, call_id="cx1"),
        execution_context=ctx,
    )
    assert obs.status == "success"
    assert not target.exists()

    result = await revert_turn("turn_x", workspace_root=workspace)
    assert result.ok
    # 字节级断言：内容（含尾换行 / CRLF）必须原样还原
    assert target.read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_write_revert_preserves_trailing_newline(isolated_storage):
    """BL-1 回归：write_file 新建带尾换行文件 → 回退（DELETE）后磁盘回到不存在。"""
    from app.service.task.turn_revert_service import revert_turn
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.write_file import build_write_file_definition
    from app.tools.tool_registry import ToolRegistry

    workspace = isolated_storage["tmp_path"] / "ws"
    workspace.mkdir()
    _seed_task_turn(workspace, "task1", "turn_w", TurnStatus.COMPLETED.value)

    registry = ToolRegistry()
    registry.register(build_write_file_definition())
    scheduler = ToolScheduler(registry)
    ctx = ToolExecutionContext(
        task_id="task1", workspace_id="ws1", workspace_root=workspace, turn_id="turn_w"
    )
    obs = scheduler.execute(
        ToolCall(
            tool_name="write_file",
            arguments={"path": "new.txt", "content": "hello\nworld\n"},
            call_id="cw1",
        ),
        execution_context=ctx,
    )
    assert obs.status == "success"
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "hello\nworld\n"

    result = await revert_turn("turn_w", workspace_root=workspace)
    assert result.ok
    # 回退将新建文件删除，磁盘回到初始（文件不存在）
    assert not (workspace / "new.txt").exists()


@pytest.mark.asyncio
async def test_write_empty_content_revert_restores_original(isolated_storage):
    """边界：write_file 把文件清空为 content='' 后回退，原内容必须复原（非空 search 跳过）。

    write_file(content='') 的采集为 modified(before=原文, after='')；反向 UPDATE 的
    search=''（after 为空）曾被 fuzzy 跳过导致虚假成功、原内容丢失。修复后空 search
    走整文件覆盖分支还原 before。
    """
    from app.service.task.turn_revert_service import revert_turn
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.write_file import build_write_file_definition
    from app.tools.tool_registry import ToolRegistry

    workspace = isolated_storage["tmp_path"] / "ws"
    workspace.mkdir()
    target = workspace / "f.txt"
    target.write_text("original-secret-data\n", encoding="utf-8")
    _seed_task_turn(workspace, "task1", "turn_e", TurnStatus.COMPLETED.value)

    registry = ToolRegistry()
    registry.register(build_write_file_definition())
    scheduler = ToolScheduler(registry)
    ctx = ToolExecutionContext(
        task_id="task1", workspace_id="ws1", workspace_root=workspace, turn_id="turn_e"
    )
    # write_file 清空文件
    obs = scheduler.execute(
        ToolCall(tool_name="write_file", arguments={"path": "f.txt", "content": ""}, call_id="ce1"),
        execution_context=ctx,
    )
    assert obs.status == "success"
    assert target.read_text(encoding="utf-8") == ""

    result = await revert_turn("turn_e", workspace_root=workspace)
    assert result.ok
    # 原内容必须精确复原，而非保持空文件（虚假成功）
    assert target.read_text(encoding="utf-8") == "original-secret-data\n"


@pytest.mark.asyncio
async def test_revert_is_idempotent_on_reentry(isolated_storage):
    """边界：部分失败后重入 revert_turn 应继续完成（可重入），不永久卡死。

    模拟手段：第一次回退前手动把首个反向操作的目标态预置为「已完成」
    （文件已重建），验证 _restore_files 能跳过已完成项并继续后续操作，最终
    磁盘态与单次完整回退一致。
    """
    from app.service.task.turn_revert_service import revert_turn
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.delete import build_delete_definition
    from app.tools.tool_registry import ToolRegistry

    workspace = isolated_storage["tmp_path"] / "ws"
    workspace.mkdir()
    target = workspace / "del.txt"
    target.write_text("recover-me\n", encoding="utf-8")
    _seed_task_turn(workspace, "task1", "turn_r", TurnStatus.COMPLETED.value)

    registry = ToolRegistry()
    registry.register(build_delete_definition())
    scheduler = ToolScheduler(registry)
    ctx = ToolExecutionContext(
        task_id="task1", workspace_id="ws1", workspace_root=workspace, turn_id="turn_r"
    )
    obs = scheduler.execute(
        ToolCall(tool_name="delete", arguments={"path": "del.txt"}, call_id="cr1"),
        execution_context=ctx,
    )
    assert obs.status == "success"
    assert not target.exists()

    # 第一次回退：重建文件
    r1 = await revert_turn("turn_r", workspace_root=workspace)
    assert r1.ok
    assert target.read_text(encoding="utf-8") == "recover-me\n"
    assert TurnCrud().get("turn_r").status == TurnStatus.REVERTED.value

    # 再次回退（重入）：状态已是 REVERTED，D6 守卫放行；文件已重建，restore 应跳过
    # 已完成的 ADD 并继续（clear 阶段幂等），整体再次成功且不报错。
    r2 = await revert_turn("turn_r", workspace_root=workspace)
    assert r2.ok
    assert target.read_text(encoding="utf-8") == "recover-me\n"


@pytest.mark.asyncio
async def test_revert_bom_file_idempotent_on_reentry(isolated_storage):
    """MA-4 回归：含 BOM 的文件删除后回退重建，重入比较不因子 BOM 前缀而卡死。

    delete 读取 before 用 bytes 解码（保留 BOM），回退重建文件带 BOM；重入时
    _is_already_reverted 用 utf-8-sig 读取剥离 BOM 后与 expected 比较，避免
    read_text 带 ``\\ufeff`` 前缀导致「未还原」误判、进而重复 apply 触发
    destination-already-exists 卡死。
    """
    from app.service.task.turn_revert_service import revert_turn
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.delete import build_delete_definition
    from app.tools.tool_registry import ToolRegistry

    workspace = isolated_storage["tmp_path"] / "ws"
    workspace.mkdir()
    target = workspace / "bom.txt"
    target.write_bytes(b"\xef\xbb\xbfhello\n")
    _seed_task_turn(workspace, "task1", "turn_b", TurnStatus.COMPLETED.value)

    registry = ToolRegistry()
    registry.register(build_delete_definition())
    scheduler = ToolScheduler(registry)
    ctx = ToolExecutionContext(
        task_id="task1", workspace_id="ws1", workspace_root=workspace, turn_id="turn_b"
    )
    obs = scheduler.execute(
        ToolCall(tool_name="delete", arguments={"path": "bom.txt"}, call_id="cb1"),
        execution_context=ctx,
    )
    assert obs.status == "success"
    assert not target.exists()

    # 第一次回退重建（带 BOM）
    r1 = await revert_turn("turn_b", workspace_root=workspace)
    assert r1.ok
    assert target.read_bytes() == b"\xef\xbb\xbfhello\n"

    # 重入：BOM 文件比较不应卡死，应成功跳过已完成的 ADD
    r2 = await revert_turn("turn_b", workspace_root=workspace)
    assert r2.ok
    assert target.read_bytes() == b"\xef\xbb\xbfhello\n"
