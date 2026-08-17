"""file_snapshot seq 命名空间修复回归测试（审查报告 P0#2）。

覆盖：seq 从「turn 内递增」修正为「task 内递增」后——
1. 同 task 跨 turn seq 单调递增、task 间命名空间隔离；
2. 跨 turn 修改同 path 时「最新变更」判定正确（撤销不再还原到旧 turn 状态）；
3. ``query_change_set`` 按 task 直查聚合、checkpoint 截断只含该 turn 及之前；
4. ``(task_id, seq)`` 唯一索引保险丝；
5. 存量数据迁移（回填 task_id + 按 task 重排 seq）。
"""

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy import text

from app.config.settings import Settings
from app.models.file_snapshot_record import FileSnapshotRecord
from app.models.turn_record import TurnRecord
from app.service.depends import reset_service_dependencies
from app.service.task.change_set_service import query_change_set
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.turn_crud import TurnCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.init_schema import _backfill_file_snapshot_task_seq
from app.storage.store_engines import close_storage, init_storage, main_engine


@pytest.fixture
def isolated_storage(tmp_path: Path) -> Iterator[None]:
    """为单个测试初始化隔离 SQLite 存储（复用 delegation 测试同款模式）。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        已初始化的隔离存储上下文。

    异常:
        OSError: 当临时 SQLite 存储无法初始化时抛出。

    副作用:
        临时覆盖进程级存储路径，并在测试结束后关闭存储、清理 service 单例并恢复配置。
    """

    original_database_file = Settings.DATABASE_FILE
    original_log_database_file = Settings.LOG_DATABASE_FILE
    original_checkpoint_file = Settings.CHECKPOINT_FILE
    reset_service_dependencies()
    close_storage()
    Settings.override(
        DATABASE_FILE=tmp_path / "app.sqlite3",
        LOG_DATABASE_FILE=tmp_path / "logs.sqlite3",
        CHECKPOINT_FILE=tmp_path / "checkpoints.sqlite3",
    )
    init_storage()
    try:
        yield
    finally:
        close_storage()
        Settings.override(
            DATABASE_FILE=original_database_file,
            LOG_DATABASE_FILE=original_log_database_file,
            CHECKPOINT_FILE=original_checkpoint_file,
        )


def _snapshot(
    task_id: str,
    turn_id: str,
    seq: int,
    path: str,
    action: str = "modified",
    stable: int = 1,
) -> FileSnapshotRecord:
    """构造一条最小快照记录，``op_json`` 用空对象占位（本组测试不执行真实撤销）。

    参数:
        task_id: 快照归属任务。
        turn_id: 快照归属轮次。
        seq: task 内序号。
        path: 相对 workspace 的文件路径。
        action: 变更动作（``created`` / ``modified`` / ``deleted``）。
        stable: 是否已稳定（1 为稳定，0 为运行中）。

    返回:
        可直接落库的 ``FileSnapshotRecord``。
    """
    return FileSnapshotRecord(
        task_id=task_id,
        turn_id=turn_id,
        tool_call_id=f"tc-{turn_id}-{seq}",
        tool_name="write_file",
        path=path,
        action=action,
        op_json="{}",
        seq=seq,
        stable=stable,
    )


def _create_turns(task_id: str, count: int) -> list[TurnRecord]:
    """创建 task 下若干轮次记录并返回按查询序的列表。

    ``TurnCrud.create`` 自动生成 turn_id，此处以 ``list_by_task`` 的返回序为准
    （``_turn_ids_until`` 与 checkpoint 截断都按该序解析），保证测试与生产消费序一致。

    参数:
        task_id: 轮次归属任务。
        count: 待创建的轮次数量。

    返回:
        该 task 下全部 ``TurnRecord``，按创建时间再 turn_id 升序。
    """
    # turns.task_id / tasks.workspace_id 外键：先建 workspace 与 task。
    workspace = WorkspaceCrud().create(
        name=f"ws-{task_id}", root_path=str(Path(tempfile.gettempdir()) / f"ws-{task_id}")
    )
    TaskCrud().create(
        task_id=task_id,
        workspace_id=workspace.workspace_id,
        agent_id="developer",
        title="test task",
        status="running",
    )
    crud = TurnCrud()
    for index in range(count):
        crud.create(task_id, f"input-{index}", status="completed")
    turns = crud.list_by_task(task_id)
    assert len(turns) == count
    return turns


def test_next_seq_increments_within_task_across_turns(isolated_storage: None) -> None:
    """同 task 跨 turn 的 seq 单调递增（修复前每 turn 从 0 开始重叠）。"""
    crud = FileSnapshotCrud()
    assert crud.next_seq("task-1") == 0
    crud.save(_snapshot("task-1", "turn-1", 0, "a.txt"))
    assert crud.next_seq("task-1") == 1
    crud.save(_snapshot("task-1", "turn-2", 1, "a.txt"))
    assert crud.next_seq("task-1") == 2


def test_seq_namespace_isolated_between_tasks(isolated_storage: None) -> None:
    """不同 task 的 seq 各自从 0 开始，跨 task 查询互不串扰。"""
    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-a", "t1", 0, "a.txt"))
    crud.save(_snapshot("task-a", "t2", 1, "b.txt"))
    crud.save(_snapshot("task-b", "t1", 0, "c.txt"))
    assert [r.seq for r in crud.list_any_by_task("task-a")] == [0, 1]
    assert [r.seq for r in crud.list_any_by_task("task-b")] == [0]
    # task-b 只能看到自己的路径
    assert crud.latest_any_by_path("task-b", "c.txt") is not None
    assert crud.latest_any_by_path("task-b", "a.txt") is None


def test_latest_by_path_prefers_latest_turn_across_same_path(isolated_storage: None) -> None:
    """核心 bug 回归：跨 turn 修改同 path，「最新」必须是后一个 turn 的快照。"""
    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-1", "turn-1", 0, "a.txt", action="created"))
    crud.save(_snapshot("task-1", "turn-2", 1, "a.txt", action="deleted"))
    latest = crud.latest_any_by_path("task-1", "a.txt")
    assert latest is not None
    assert latest.turn_id == "turn-2"
    assert latest.action == "deleted"
    stable_latest = crud.latest_stable_by_path("task-1", "a.txt")
    assert stable_latest is not None
    assert stable_latest.turn_id == "turn-2"


def test_query_change_set_dedup_keeps_latest_turn(isolated_storage: None) -> None:
    """``query_change_set`` 聚合按 task 直查，同 path 保留最新 turn 的变更。"""
    turns = _create_turns("task-1", 2)
    turn_1, turn_2 = turns[0].turn_id, turns[1].turn_id
    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-1", turn_1, 0, "a.txt", action="created"))
    crud.save(_snapshot("task-1", turn_2, 1, "a.txt", action="modified"))
    result = query_change_set("task-1", include_running=False)
    assert len(result.checkpoints) == 2
    assert len(result.files) == 1
    entry = result.files[0]
    assert entry.path == "a.txt"
    assert entry.action == "modified"
    assert entry.last_turn_id == turn_2


def test_query_change_set_checkpoint_truncates_by_turn(isolated_storage: None) -> None:
    """checkpoint 截断：只聚合到指定 turn（含）为止，但检查点列表完整返回。"""
    turns = _create_turns("task-1", 2)
    turn_1, turn_2 = turns[0].turn_id, turns[1].turn_id
    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-1", turn_1, 0, "a.txt", action="created"))
    crud.save(_snapshot("task-1", turn_2, 1, "a.txt", action="modified"))
    result = query_change_set("task-1", checkpoint_turn_id=turn_1, include_running=False)
    assert len(result.checkpoints) == 2
    assert result.files[0].last_turn_id == turn_1


def test_unique_task_seq_index_guards_duplicate(isolated_storage: None) -> None:
    """``(task_id, seq)`` 唯一索引保险丝：同 task 重复 seq 拒绝落库。"""
    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-1", "turn-1", 0, "a.txt"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        crud.save(_snapshot("task-1", "turn-2", 0, "b.txt"))
    # 不同 task 同 seq 合法（命名空间隔离）
    crud.save(_snapshot("task-2", "turn-1", 0, "c.txt"))


def test_backfill_migration_fixes_legacy_seq(isolated_storage: None) -> None:
    """存量迁移：回填 task_id + 按 task 重排重叠 seq，使「最新变更」判定在旧数据上同样可靠。"""
    turns = _create_turns("task-1", 2)
    turn_1, turn_2 = turns[0].turn_id, turns[1].turn_id
    # 模拟旧 schema：删除保险丝唯一索引（旧库无该索引），才允许插入重叠 seq 的旧数据。
    with main_engine().begin() as connection:
        connection.execute(text("DROP INDEX uq_file_snapshots_task_seq"))
    crud = FileSnapshotCrud()
    # 旧版语义：同 task 不同 turn 的 seq 都从 0 开始（每 turn 独立递增）。
    crud.save(_snapshot("task-1", turn_1, 0, "a.txt", action="created"))
    crud.save(_snapshot("task-1", turn_2, 0, "a.txt", action="modified"))
    # 抹掉 task_id，模拟旧表缺该列（存量库回填前所有行 task_id 为空串）。
    with main_engine().begin() as connection:
        connection.execute(text("UPDATE file_snapshots SET task_id = ''"))
    # 运行迁移（真实初始化流程里排在 _ensure_model_indexes 之前）。
    with main_engine().begin() as connection:
        _backfill_file_snapshot_task_seq(connection)
    rows = crud.list_any_by_task("task-1")
    assert [r.seq for r in rows] == [0, 1]
    assert all(r.task_id == "task-1" for r in rows)
    latest = crud.latest_any_by_path("task-1", "a.txt")
    assert latest is not None
    assert latest.turn_id == turn_2
    assert latest.action == "modified"
    # 迁移后 (task_id, seq) 已唯一，保险丝索引可重建。
    with main_engine().begin() as connection:
        connection.execute(
            text(
                "CREATE UNIQUE INDEX uq_file_snapshots_task_seq "
                "ON file_snapshots (task_id, seq)"
            )
        )
