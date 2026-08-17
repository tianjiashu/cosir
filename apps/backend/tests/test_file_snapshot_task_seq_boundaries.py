"""file_snapshot seq 命名空间修复——补充边界回归测试（审查后新增）。

在 ``test_file_snapshot_task_seq.py`` 的 7 个用例之外，补齐审查发现的关键边界：

1. **迁移幂等**：``_backfill_file_snapshot_task_seq`` 二次运行不得改变任何行的
   ``task_id`` / ``seq``（docstring 声明的幂等契约）；
2. **迁移孤儿清理**：所属 turn 已被删除的快照在迁移时被清理（跨 task 归因前置）；
3. **并发隔离**：``next_seq``（MAX+1 读改写）非原子，并发下同 task 可能拿到相同
   seq——``(task_id, seq)`` 唯一索引作为保险丝拒绝重复落库，库内最终不变量
   「无重复 (task_id, seq)」必须成立；
4. **checkpoint 非法 turn**：``query_change_set`` 传入不属于该 task 的
   ``checkpoint_turn_id`` 必须抛 ``ValueError``；
5. **include_running 分支**：运行中（``stable=0``）快照是否参与「最新变更」聚合；
6. **CRUD 边界**：空 ``turn_ids`` 短路、单 turn 回放 seq 降序、``mark_stable_by_turn``
   幂等与跨 turn 隔离、``update_status`` CAS miss、``clear_by_turn`` 范围。

另含迁移防御分支（``file_snapshots`` / ``turns`` 表缺失、空表 noop）与
``revert_file`` / ``keep_file`` / ``_resolve_workspace_root`` 端到端用例。

本文件只包含测试，不修改任何业务代码。
"""

import tempfile
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy import text

from app.config.settings import Settings
from app.models.file_snapshot_record import FileSnapshotRecord
from app.models.turn_record import TurnRecord
from app.service.depends import reset_service_dependencies
from app.service.task.change_set_service import (
    ChangeSetConflictError,
    _cas_update_status,
    _require_latest_any,
    query_change_set,
)
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.turn_crud import TurnCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.init_schema import _backfill_file_snapshot_task_seq
from app.storage.store_engines import close_storage, init_storage, main_engine


@pytest.fixture
def isolated_storage(tmp_path: Path) -> Iterator[None]:
    """为单个测试初始化隔离 SQLite 存储（与 test_file_snapshot_task_seq 同款模式）。"""

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
    """构造一条最小快照记录（op_json 用空对象占位，本组测试不执行真实撤销）。"""

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
    """创建 task 下若干轮次记录并返回按查询序（创建时间再 turn_id）的列表。"""

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


def _simulate_legacy_schema(crud: FileSnapshotCrud, snapshots: list[FileSnapshotRecord]) -> None:
    """模拟旧 schema：无唯一索引、无 task_id 列（存量行 task_id 抹空）后插入旧数据。"""

    with main_engine().begin() as connection:
        connection.execute(text("DROP INDEX IF EXISTS uq_file_snapshots_task_seq"))
    for snap in snapshots:
        crud.save(snap)
    with main_engine().begin() as connection:
        connection.execute(text("UPDATE file_snapshots SET task_id = ''"))


def test_backfill_migration_is_idempotent_on_second_run(isolated_storage: None) -> None:
    """迁移幂等：二次运行 _backfill_file_snapshot_task_seq 不得改变任何行 (task_id, seq)。"""
    turns = _create_turns("task-1", 2)
    turn_1, turn_2 = turns[0].turn_id, turns[1].turn_id
    crud = FileSnapshotCrud()
    # 旧版语义：同 task 不同 turn 的 seq 都从 0 开始（每 turn 独立递增）。
    _simulate_legacy_schema(
        crud,
        [
            _snapshot("task-1", turn_1, 0, "a.txt", action="created"),
            _snapshot("task-1", turn_2, 0, "a.txt", action="modified"),
            _snapshot("task-1", turn_2, 1, "b.txt", action="created"),
        ],
    )

    with main_engine().begin() as connection:
        _backfill_file_snapshot_task_seq(connection)
    first = crud.list_any_by_task("task-1")
    assert [r.seq for r in first] == [0, 1, 2]
    assert all(r.task_id == "task-1" for r in first)
    first_state = [(r.id, r.task_id, r.seq, r.path, r.action) for r in first]

    # 二次运行：幂等契约，任何行都不许被重排。
    with main_engine().begin() as connection:
        _backfill_file_snapshot_task_seq(connection)
    second = crud.list_any_by_task("task-1")
    second_state = [(r.id, r.task_id, r.seq, r.path, r.action) for r in second]
    assert second_state == first_state
    assert [r.seq for r in second] == [0, 1, 2]


def test_backfill_migration_deletes_orphan_snapshots(isolated_storage: None) -> None:
    """迁移孤儿清理：所属 turn 已被删除的快照在迁移时被删除，正常行照常回填。"""
    turns = _create_turns("task-1", 1)
    turn_1 = turns[0].turn_id
    crud = FileSnapshotCrud()
    _simulate_legacy_schema(
        crud,
        [
            _snapshot("task-1", turn_1, 0, "a.txt", action="created"),
            _snapshot("task-1", turn_1, 0, "b.txt", action="modified"),
        ],
    )
    # 孤儿快照：turn_id 指向不存在的 turn，无法归因任何 task。
    crud.save(_snapshot("task-1", "no-such-turn", 0, "orphan.txt", action="created"))
    with main_engine().begin() as connection:
        connection.execute(text("UPDATE file_snapshots SET task_id = ''"))

    with main_engine().begin() as connection:
        _backfill_file_snapshot_task_seq(connection)

    rows = crud.list_any_by_task("task-1")
    assert [r.seq for r in rows] == [0, 1]
    assert all(r.task_id == "task-1" for r in rows)
    with main_engine().begin() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM file_snapshots WHERE turn_id = 'no-such-turn'")
        ).scalar_one()
    assert count == 0


def test_concurrent_seq_allocation_unique_index_fuse(isolated_storage: None) -> None:
    """并发隔离：多线程并发 next_seq+save，唯一索引拒绝重复 (task_id, seq)，库内不变量成立。"""
    task_id = "task-concurrent"
    crud = FileSnapshotCrud()
    barrier = threading.Barrier(8)

    def worker(_: int) -> None:
        barrier.wait()
        seq = crud.next_seq(task_id)
        crud.save(_snapshot(task_id, f"turn-{_}", seq, f"f-{_}.txt"))

    errors: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker, i) for i in range(8)]
        for future in futures:
            try:
                future.result()
            except Exception as exc:
                errors.append(exc)

    rows = crud.list_any_by_task(task_id)
    pairs = [(r.task_id, r.seq) for r in rows]
    # 核心不变量：至少 1 行成功落库（防空洞通过），且 (task_id, seq) 必须唯一
    # （不允许「重复 seq 静默落库」）。
    assert len(rows) >= 1
    assert len(pairs) == len(set(pairs)), "并发落库后出现重复 (task_id, seq)"
    assert len(rows) <= 8
    # 失败路径仅接受两种预期异常：IntegrityError（唯一索引保险丝拦截）与
    # OperationalError（SQLite 写竞争 database locked）；出现其他异常类型即回归。
    assert all(
        isinstance(e, sqlalchemy.exc.IntegrityError | sqlalchemy.exc.OperationalError)
        for e in errors
    ), f"并发下出现未预期的异常类型: {errors}"


def test_query_change_set_rejects_foreign_checkpoint_turn(isolated_storage: None) -> None:
    """checkpoint 非法 turn：checkpoint_turn_id 不属于该 task 时必须抛 ValueError。"""
    _create_turns("task-1", 2)
    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-1", "t1", 0, "a.txt", action="created"))

    with pytest.raises(ValueError):
        query_change_set("task-1", checkpoint_turn_id="foreign-turn", include_running=False)


def test_query_change_set_include_running_keeps_unstable_latest(isolated_storage: None) -> None:
    """include_running 分支：运行中（stable=0）的更新必须覆盖已稳定旧态，反之只取稳定态。"""
    turns = _create_turns("task-1", 2)
    turn_1, turn_2 = turns[0].turn_id, turns[1].turn_id
    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-1", turn_1, 0, "a.txt", action="created", stable=1))
    crud.save(_snapshot("task-1", turn_2, 1, "a.txt", action="deleted", stable=0))

    running = query_change_set("task-1", include_running=True)
    assert len(running.files) == 1
    assert running.files[0].action == "deleted"
    assert running.files[0].last_turn_id == turn_2

    stable_only = query_change_set("task-1", include_running=False)
    assert len(stable_only.files) == 1
    assert stable_only.files[0].action == "created"
    assert stable_only.files[0].last_turn_id == turn_1


def test_crud_empty_turn_filter_short_circuits(isolated_storage: None) -> None:
    """CRUD 边界：空 turn_ids 短路——list_* 返回空、latest_* 返回 None。"""
    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-1", "turn-1", 0, "a.txt", action="created"))
    assert crud.list_stable_by_task("task-1", []) == []
    assert crud.list_any_by_task("task-1", []) == []
    assert crud.latest_stable_by_path("task-1", "a.txt", []) is None
    assert crud.latest_any_by_path("task-1", "a.txt", []) is None


def test_list_by_turn_orders_seq_desc_within_turn(isolated_storage: None) -> None:
    """单 turn 回放：同 turn 快照按 seq 降序（回退时逆序应用），跨 turn 不串扰。"""
    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-1", "turn-1", 0, "a.txt"))
    crud.save(_snapshot("task-1", "turn-1", 1, "b.txt"))
    crud.save(_snapshot("task-1", "turn-1", 2, "c.txt"))
    # 真实场景 turn_id 为全局唯一 UUID；跨 task 用不同 turn_id，seq 各自从 0 起不冲突。
    crud.save(_snapshot("task-2", "turn-1-b", 0, "z.txt"))

    seqs = [r.seq for r in crud.list_by_turn("turn-1")]
    assert seqs == [2, 1, 0]
    assert [r.path for r in crud.list_by_turn("turn-1")] == ["c.txt", "b.txt", "a.txt"]
    # 其它 turn 的快照不在结果中（按 turn_id 精确匹配）。
    assert len(crud.list_by_turn("turn-1")) == 3
    assert len(crud.list_by_turn("turn-1-b")) == 1


def test_mark_stable_by_turn_idempotent_and_isolated(isolated_storage: None) -> None:
    """mark_stable_by_turn：只标记目标 turn 的 stable=0 行；重复调用幂等返回 0。"""
    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-1", "turn-1", 0, "a.txt", stable=0))
    crud.save(_snapshot("task-1", "turn-1", 1, "b.txt", stable=0))
    # task 内 seq 唯一：turn-2 的新快照 seq 必须继续递增（task 命名空间）。
    crud.save(_snapshot("task-1", "turn-2", 2, "c.txt", stable=0))

    assert crud.mark_stable_by_turn("turn-1") == 2
    assert crud.mark_stable_by_turn("turn-1") == 0  # 幂等：已稳定行不重复计入
    assert all(r.stable == 1 for r in crud.list_any_by_task("task-1", ["turn-1"]))
    # 跨 turn 隔离：turn-2 的快照不被顺带标记。
    turn2 = crud.list_any_by_task("task-1", ["turn-2"])
    assert len(turn2) == 1 and turn2[0].stable == 0


def test_update_status_cas_rejects_concurrent_mutation(isolated_storage: None) -> None:
    """update_status CAS：当前 status 已被并发改掉时，条件更新返回 0（不盲写）。"""
    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-1", "turn-1", 0, "a.txt"))
    row = crud.list_any_by_task("task-1")[0]

    assert crud.update_status(row.id, "kept", expected_statuses=("pending",)) == 1
    # 已被改成 kept，再以 pending 为期望则 CAS miss。
    assert crud.update_status(row.id, "reverted", expected_statuses=("pending",)) == 0
    # 无条件更新（不传 expected）保持旧语义，可覆盖。
    assert crud.update_status(row.id, "reverted") == 1


def test_clear_by_turn_removes_only_that_turn(isolated_storage: None) -> None:
    """clear_by_turn：只删除目标 turn 的快照，其它 turn / 其它 task 不受影响。"""
    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-1", "turn-1", 0, "a.txt"))
    crud.save(_snapshot("task-1", "turn-1", 1, "b.txt"))
    # task 内 seq 唯一：turn-2 的新快照 seq 必须继续递增（task 命名空间）。
    crud.save(_snapshot("task-1", "turn-2", 2, "c.txt"))
    # 真实场景 turn_id 为全局唯一 UUID，跨 task 不会复用同一 turn_id。
    crud.save(_snapshot("task-2", "turn-1-b", 0, "d.txt"))

    crud.clear_by_turn("turn-1")

    assert crud.list_any_by_task("task-1", ["turn-1"]) == []
    assert len(crud.list_any_by_task("task-1", ["turn-2"])) == 1
    assert len(crud.list_any_by_task("task-2")) == 1


def test_cas_update_status_raises_conflict_on_miss(isolated_storage: None) -> None:
    """change_set_service._cas_update_status：CAS miss 必须抛 ChangeSetConflictError(409)。"""
    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-1", "turn-1", 0, "a.txt"))
    row = crud.list_any_by_task("task-1")[0]

    _cas_update_status(row.id, "kept", "task-1", "a.txt", row.turn_id)
    with pytest.raises(ChangeSetConflictError):
        _cas_update_status(row.id, "reverted", "task-1", "a.txt", row.turn_id)


def test_require_latest_any_raises_when_no_change(isolated_storage: None) -> None:
    """change_set_service._require_latest_any：path 无任何变更（含运行中）时抛 ValueError。"""
    with pytest.raises(ValueError):
        _require_latest_any("task-1", "no-such-file.txt")


def test_latest_by_path_filters_by_turn_ids(isolated_storage: None) -> None:
    """latest_*_by_path 传非空 turn_ids：只在该 turn 子集内取最新，跨 turn 的最新不被选中。"""
    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-1", "turn-1", 0, "a.txt", action="created"))
    crud.save(_snapshot("task-1", "turn-2", 1, "a.txt", action="modified"))

    latest_all = crud.latest_any_by_path("task-1", "a.txt")
    assert latest_all is not None and latest_all.turn_id == "turn-2"
    # 截断到 turn-1：最新是 turn-1 的 created（seq 命名空间下按 task 直查 + turn 过滤）。
    latest_turn1 = crud.latest_any_by_path("task-1", "a.txt", ["turn-1"])
    assert latest_turn1 is not None and latest_turn1.turn_id == "turn-1"
    assert latest_turn1.action == "created"
    stable_turn1 = crud.latest_stable_by_path("task-1", "a.txt", ["turn-1"])
    assert stable_turn1 is not None and stable_turn1.action == "created"


def test_backfill_migration_noop_when_table_missing(isolated_storage: None) -> None:
    """迁移防御分支：file_snapshots 表不存在时 _backfill 直接返回，不抛错。"""
    with main_engine().begin() as connection:
        connection.execute(text("DROP TABLE file_snapshots"))
    with main_engine().begin() as connection:
        _backfill_file_snapshot_task_seq(connection)  # 不应抛异常


def test_backfill_migration_noop_when_turns_table_missing(isolated_storage: None) -> None:
    """迁移防御分支：turns 表不存在（file_snapshots 存在）时 _backfill 直接返回，不抛错。"""
    with main_engine().begin() as connection:
        connection.execute(text("DROP TABLE turns"))
    with main_engine().begin() as connection:
        _backfill_file_snapshot_task_seq(connection)  # 不应抛异常


def test_backfill_migration_noop_on_empty_table(isolated_storage: None) -> None:
    """迁移防御分支：表存在但无任何快照行时 _backfill 直接返回，不抛错。"""
    with main_engine().begin() as connection:
        _backfill_file_snapshot_task_seq(connection)


def test_keep_file_marks_status_kept(isolated_storage: None) -> None:
    """keep_file：把 path 最新变更标记为 kept，不触碰磁盘、不解析 op_json。"""
    from app.service.task.change_set_service import keep_file

    crud = FileSnapshotCrud()
    crud.save(_snapshot("task-1", "turn-1", 0, "a.txt", action="modified"))
    entry = keep_file("task-1", "a.txt")
    assert entry.path == "a.txt"
    assert entry.status == "kept"
    row = crud.list_any_by_task("task-1")[0]
    assert row.status == "kept"


async def test_revert_file_deletes_file_end_to_end(
    isolated_storage: None, tmp_path: Path
) -> None:
    """revert_file 端到端：反向 DELETE op 把 workspace 内文件删除并标记 reverted。

    覆盖 change_set_service 的 R6 判定、apply_all_with_diff、CAS、撤销后广播、
    _snapshots_to_operations 解析全链路。
    """
    from app.service.task.change_set_service import revert_file

    target = tmp_path / "a.txt"
    target.write_text("content", encoding="utf-8")
    crud = FileSnapshotCrud()
    # 反向 DELETE op（原 ADD）：撤销 = 删除磁盘文件。
    crud.save(
        FileSnapshotRecord(
            task_id="task-1",
            turn_id="turn-1",
            tool_call_id="tc-1",
            tool_name="write_file",
            path="a.txt",
            action="created",
            op_json=(
                '{"operation": "delete", "file_path": "a.txt", '
                '"new_path": null, "hunks": [], "content": null}'
            ),
            seq=0,
            stable=1,
        )
    )

    entry = await revert_file("task-1", "a.txt", workspace_root=tmp_path)

    assert not target.exists()
    assert entry.status == "reverted"
    assert entry.action == "created"
    row = crud.list_any_by_task("task-1")[0]
    assert row.status == "reverted"
    assert row.reverted_at != ""


async def test_revert_file_rejects_manual_modification(
    isolated_storage: None, tmp_path: Path
) -> None:
    """revert_file 软冲突防护：磁盘内容既非 before 也非 after（用户手动改过）时拒绝撤销。"""
    from app.service.task.change_set_service import revert_file
    from app.tools.tool_handler.patch.patch_apply import PatchApplyError

    target = tmp_path / "b.txt"
    target.write_text("user-modified-content", encoding="utf-8")
    crud = FileSnapshotCrud()
    # 反向 UPDATE op：content = before 态，hunks '-' 行 = after 态（Agent 改完态）。
    crud.save(
        FileSnapshotRecord(
            task_id="task-1",
            turn_id="turn-1",
            tool_call_id="tc-2",
            tool_name="write_file",
            path="b.txt",
            action="modified",
            op_json=(
                '{"operation": "update", "file_path": "b.txt", "new_path": null, '
                '"content": "before-content", '
                '"hunks": [{"lines": [{"prefix": "-", "content": "after-content"}]}]}'
            ),
            seq=0,
            stable=1,
        )
    )

    with pytest.raises(PatchApplyError):
        await revert_file("task-1", "b.txt", workspace_root=tmp_path)

    # 拒绝撤销时不改写状态、不删文件。
    assert target.exists()
    row = crud.list_any_by_task("task-1")[0]
    assert row.status == "pending"


def test_resolve_workspace_root_raises_for_unknown_task(isolated_storage: None) -> None:
    """_resolve_workspace_root：task 不存在时异常向上冒泡（不可静默返回兜底路径）。"""
    from app.service.task.change_set_service import _resolve_workspace_root

    with pytest.raises(KeyError):
        _resolve_workspace_root("no-such-task")
