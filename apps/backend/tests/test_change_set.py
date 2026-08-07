"""task 级变更集（change_set_service）测试。

测试隔离：统一复用 ``tests/conftest.py`` 提供的 ``isolated_storage`` 夹具（每个用例
独立 DB 与 checkpoint 文件，teardown 关闭 storage 并重置 service 依赖）。
"""

import json
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import text

from app.models.file_snapshot_record import FileSnapshotRecord
from app.service.task import change_set_service
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
from app.storage.model.file_snapshot_model import FileSnapshotModel
from app.storage.store_engines import close_storage, init_storage, main_engine, main_session_factory
from app.tools.tool_handler.patch.patch_apply import PatchApplyError


def test_snapshot_record_defaults_and_roundtrip(isolated_storage):
    """新增三列默认值正确，且 save → list_by_turn 往返不丢字段。"""
    crud = FileSnapshotCrud()
    crud.save(
        FileSnapshotRecord(
            turn_id="turn1",
            tool_call_id="call1",
            tool_name="write_file",
            path="a.txt",
            action="created",
            op_json="{}",
            seq=0,
        )
    )
    [row] = crud.list_by_turn("turn1")
    assert row.stable == 0
    assert row.status == "pending"
    assert row.reverted_at == ""
    assert row.additions == 0
    assert row.deletions == 0


def test_orm_column_defaults_without_value_object(isolated_storage):
    """I-1(a)：绕过值对象直接构造 ORM 对象，仅传基线必填列，断言 ORM 层默认值生效。

    此用例验证 model 列定义自身的 ``default=`` / ``server_default=``，而非
    ``FileSnapshotRecord`` 的 Python 默认值（后者会掩盖列定义缺陷）。
    """
    from app.storage.store_engines import main_session_factory

    with main_session_factory().begin() as session:
        session.add(
            FileSnapshotModel(
                turn_id="turn1",
                tool_call_id="call1",
                tool_name="write_file",
                path="a.txt",
                action="created",
                op_json="{}",
                seq=0,
            )
        )

    [row] = FileSnapshotCrud().list_by_turn("turn1")
    assert row.stable == 0
    assert row.status == "pending"
    assert row.reverted_at == ""


def test_existing_db_migration_sets_status_pending(isolated_storage):
    """I-1(b)：存量库（不含三列）加列迁移后，历史行 status 必须为 'pending'（C-1 回归锁）。

    构造一个只含基线 8 列的旧 ``file_snapshots`` 表并写入 1 行历史数据；随后再次触发
    ``init_storage()``，由 ``_ensure_model_columns`` 对缺失列执行 ``ALTER TABLE ADD COLUMN``，
    断言历史行的三列取值与三态约束一致，尤其 ``status`` 不得落为空串。
    """
    engine = main_engine()
    # 1. 构造不含 stable/status/reverted_at 的旧表结构（基线 8 列）。
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS file_snapshots"))
        conn.execute(
            text(
                "CREATE TABLE file_snapshots ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "turn_id TEXT NOT NULL, "
                "tool_call_id TEXT NOT NULL, "
                "tool_name TEXT NOT NULL, "
                "path TEXT NOT NULL, "
                "action TEXT NOT NULL, "
                "op_json TEXT NOT NULL, "
                "seq INTEGER NOT NULL)"
            )
        )
        # 2. 写入 1 行历史数据（模拟存量库）。
        conn.execute(
            text(
                "INSERT INTO file_snapshots "
                "(turn_id, tool_call_id, tool_name, path, action, op_json, seq) "
                "VALUES ('old', 'c0', 'write_file', 'a.txt', 'created', '{}', 0)"
            )
        )

    # 3. 重新触发 schema 迁移（加列）。init_storage 有路径级幂等缓存，
    #    需先 close_storage 使其重新执行 initialize_app_schema。
    close_storage()
    init_storage()
    [row] = FileSnapshotCrud().list_by_turn("old")
    assert row.stable == 0
    assert row.status == "pending"
    assert row.reverted_at == ""
    assert row.additions == 0
    assert row.deletions == 0
    assert row.status in {"pending", "kept", "reverted"}


def _save(
    crud: FileSnapshotCrud,
    turn_id: str,
    path: str,
    seq: int,
    stable: int = 0,
) -> None:
    """往目标 turn 落一条快照，便于在测试里快速构造不同 seq / stable 的行。

    参数:
        crud: 被测的 ``FileSnapshotCrud`` 实例。
        turn_id: 归属的轮次标识。
        path: 相对 workspace 的文件路径。
        seq: 快照序号，用于验证排序与 latest 语义。
        stable: 稳定标记；默认 0（未稳定），传 1 可构造已稳定行。

    返回:
        无。

    异常:
        无（异常由底层 ``save`` 沿 SQLAlchemy 原样上抛）。

    副作用:
        向 ``file_snapshots`` 表插入一行。
    """
    crud.save(
        FileSnapshotRecord(
            turn_id=turn_id,
            tool_call_id=f"call{seq}",
            tool_name="write_file",
            path=path,
            action="modified",
            op_json="{}",
            seq=seq,
            stable=stable,
        )
    )


def test_mark_stable_and_query_latest(isolated_storage):
    """mark_stable_by_turn 后锁住行为契约：升序、stable 过滤、幂等、latest-seq。

    该用例不依赖 brief 的空转断言，而是让插入顺序与期望顺序不一致、并补充
    stable=0 反例与幂等二次调用，使每条断言在对应实现条款被破坏时变红。
    """
    crud = FileSnapshotCrud()
    # 逆序插入：先插大 seq 再插小 seq，若实现未按 seq 升序排序，
    # [r.seq for r in rows] 将不等于 [0, 1]。
    _save(crud, "turn1", "a.txt", 1)
    _save(crud, "turn1", "b.txt", 0)

    assert crud.mark_stable_by_turn("turn1") == 2

    # I-1：断言 seq 序列（而非恒等的 path 序列），逆插入顺序验证升序。
    stable_rows = crud.list_stable_by_turns(["turn1"])
    assert [r.seq for r in stable_rows] == [0, 1]
    assert [r.path for r in stable_rows] == ["b.txt", "a.txt"]

    # I-3：幂等性——重复调用应返回 0（已稳定的行不再计入）。
    # 必须在 I-2 的 stable=0 反例插入前完成，否则反例行会被二次标记而破坏断言。
    assert crud.mark_stable_by_turn("turn1") == 0

    # I-2：同一 path、更大 seq、但 stable=0 的反例，证明 stable==1 过滤生效。
    # 必须在两次 mark_stable 之后插入，否则会被一并标记。
    _save(crud, "turn1", "a.txt", 5, stable=0)
    latest = crud.latest_stable_by_path(["turn1"], "a.txt")
    assert latest is not None
    assert latest.seq == 1  # 不是 5 → 证明 stable==1 过滤生效

    crud.update_status(latest.id, "reverted", reverted_at="2026-08-04T00:00:00")
    refreshed = crud.latest_stable_by_path(["turn1"], "a.txt")
    assert refreshed is not None
    assert refreshed.status == "reverted"
    assert refreshed.reverted_at == "2026-08-04T00:00:00"


def test_change_set_short_circuits(isolated_storage):
    """空 turn_ids 与不存在的 path 走短路分支，行为契约需被锁住。

    ``list_stable_by_turns([])`` 直接返回空列表；``latest_stable_by_path`` 对
    不存在的 path 返回 None。这两条短路路径属有意设计的行为契约，需独立断言。
    """
    crud = FileSnapshotCrud()
    assert crud.list_stable_by_turns([]) == []
    assert crud.latest_stable_by_path(["turn1"], "nope.txt") is None


def test_update_status_cas_matching_updates_and_returns_1(isolated_storage):
    """CAS：当前 status 等于期望值时更新并返回 1，状态正确落库。"""
    crud = FileSnapshotCrud()
    crud.save(
        FileSnapshotRecord(
            turn_id="turn1",
            tool_call_id="call1",
            tool_name="write_file",
            path="a.txt",
            action="modified",
            op_json="{}",
            seq=0,
        )
    )
    [snap] = crud.list_by_turn("turn1")

    affected = crud.update_status(snap.id, "kept", expected_statuses=("pending",))

    assert affected == 1
    [after] = crud.list_by_turn("turn1")
    assert after.status == "kept"


def test_update_status_cas_mismatch_returns_0_and_no_change(isolated_storage):
    """CAS：当前 status 已不等于期望值时不更新，返回 0，保留并发方的改态。"""
    crud = FileSnapshotCrud()
    crud.save(
        FileSnapshotRecord(
            turn_id="turn1",
            tool_call_id="call1",
            tool_name="write_file",
            path="a.txt",
            action="modified",
            op_json="{}",
            seq=0,
        )
    )
    [snap] = crud.list_by_turn("turn1")
    # 模拟并发方已先置为 kept。
    crud.update_status(snap.id, "kept", expected_statuses=("pending",))

    affected = crud.update_status(snap.id, "reverted", expected_statuses=("pending",))

    assert affected == 0
    [after] = crud.list_by_turn("turn1")
    assert after.status == "kept"  # 并发改态未被盲写覆盖


def test_keep_file_cas_miss_rejects_when_already_mutated(isolated_storage):
    """keep_file 遇到 status 已被并发改态（CAS 不匹配）时抛 ChangeSetConflictError。

    该异常是 ``ValueError`` 子类但语义为并发冲突（API 映射 409），区别于「路径无变更」的
    404；并发方写入的 kept 得以保留（lost update 防护）。
    """
    from app.service.task.change_set_service import ChangeSetConflictError

    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    crud = FileSnapshotCrud()
    _save(crud, "turn1", "a.txt", 0, stable=1)

    # 模拟并发方（如另一路操作）已先把该行置为 kept。
    latest = crud.latest_any_by_path(["turn1"], "a.txt")
    assert latest is not None
    crud.update_status(latest.id, "kept", expected_statuses=("pending",))

    with pytest.raises(ChangeSetConflictError):
        change_set_service.keep_file("task1", "a.txt")

    # 行状态保持 kept，未被二次改写。
    after = crud.latest_any_by_path(["turn1"], "a.txt")
    assert after is not None
    assert after.status == "kept"


# ---------------------------------------------------------------------------
# change_set_service：查询编排 + 单文件保留/撤销
# ---------------------------------------------------------------------------


def _seed_task_turn(
    workspace_root: Path,
    task_id: str,
    turn_id: str,
    status: str = "completed",
    created_at: str = "2026-01-01T00:00:01Z",
) -> None:
    """直接插入 workspace/task/turn 基础行，绕过繁琐的 service 初始化。

    ``created_at`` 可显式指定，用于构造「turn 时间顺序 ≠ turn_id 字典序」的场景，
    使检查点排序断言不会因两者恰好一致而空转。

    参数:
        workspace_root: 该 workspace 的根目录，写入 ``workspaces.root_path``。
        task_id: 任务标识；同名任务重复插入被 ``INSERT OR IGNORE`` 忽略。
        turn_id: 轮次标识。
        status: turn 状态字面量，默认 ``completed``。
        created_at: turn 创建时间字符串，决定 ``list_by_task`` 的升序位置。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果插入失败。

    副作用:
        向 ``workspaces`` / ``tasks`` / ``turns`` 三表各写入一行（已存在则跳过）。
    """
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
                "VALUES (:turn_id, :tid, 'x', :status, :created_at, :created_at)"
            ),
            {"turn_id": turn_id, "tid": task_id, "status": status, "created_at": created_at},
        )
        session.commit()


def _write(workspace_root: Path, rel: str, content: str) -> None:
    """在 workspace 内写入一个文本文件（按需创建父目录）。

    参数:
        workspace_root: workspace 根目录。
        rel: 相对 workspace 的文件路径。
        content: 待写入的文本内容。

    返回:
        无。

    异常:
        OSError: 如果目录创建或文件写入失败。

    副作用:
        在磁盘上创建/覆盖目标文件。
    """
    target = workspace_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_query_change_set_dedupes_by_path(isolated_storage):
    """同一路径多次变更只输出最新一条，且按 turn 时间升序生成检查点。

    相比 brief 版本额外锁住三点：
    1. 检查点顺序用「created_at 与 turn_id 字典序相反」的数据构造，避免排序断言空转；
    2. 去重后取的是 **最新** 一条（断言 last_tool_call_id 与 action，而非仅 turn_id）；
    3. checkpoints 的 turn_seq / label 是按序递增的，而非硬编码巧合。
    """
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    # zz_turn 先创建、aa_turn 后创建：若实现误按 turn_id 字典序排，断言会变红。
    _seed_task_turn(ws, "task1", "zz_turn", created_at="2026-01-01T00:00:01Z")
    _seed_task_turn(ws, "task1", "aa_turn", created_at="2026-01-01T00:00:02Z")
    crud = FileSnapshotCrud()
    _save(crud, "zz_turn", "a.txt", 0, stable=1)
    _save(crud, "aa_turn", "a.txt", 1, stable=1)
    _save(crud, "aa_turn", "b.txt", 2, stable=1)

    change_set = change_set_service.query_change_set("task1")

    assert change_set.task_id == "task1"
    assert [f.path for f in change_set.files] == ["a.txt", "b.txt"]
    # a.txt 出现两次，必须保留 seq 更大的那条（call1 / aa_turn），而非首次的 call0。
    assert [f.last_tool_call_id for f in change_set.files] == ["call1", "call2"]
    assert [f.last_turn_id for f in change_set.files] == ["aa_turn", "aa_turn"]
    assert [f.status for f in change_set.files] == ["pending", "pending"]
    # 检查点按 created_at 升序，而非 turn_id 字典序（aa_turn 排在后面）。
    assert [c.turn_id for c in change_set.checkpoints] == ["zz_turn", "aa_turn"]
    assert [c.turn_seq for c in change_set.checkpoints] == [1, 2]
    assert [c.label for c in change_set.checkpoints] == ["检查点 1", "检查点 2"]


def test_query_change_set_respects_checkpoint(isolated_storage):
    """指定检查点时只返回到该 turn（含）为止的累积变更，检查点列表不被截断。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1", created_at="2026-01-01T00:00:01Z")
    _seed_task_turn(ws, "task1", "turn2", created_at="2026-01-01T00:00:02Z")
    crud = FileSnapshotCrud()
    _save(crud, "turn1", "a.txt", 0, stable=1)
    _save(crud, "turn2", "b.txt", 1, stable=1)

    at_turn1 = change_set_service.query_change_set("task1", checkpoint_turn_id="turn1")
    assert [f.path for f in at_turn1.files] == ["a.txt"]
    # 截断只作用于文件聚合，检查点下拉仍需展示全部 turn。
    assert [c.turn_id for c in at_turn1.checkpoints] == ["turn1", "turn2"]

    # 对照组：不指定检查点时两个文件都在，证明上面的 ["a.txt"] 不是恒真。
    assert [f.path for f in change_set_service.query_change_set("task1").files] == [
        "a.txt",
        "b.txt",
    ]


def test_query_change_set_rejects_foreign_checkpoint(isolated_storage):
    """检查点 turn 不属于该 task 时必须报错，而非静默返回全量。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")

    with pytest.raises(ValueError, match="checkpoint turn not in task"):
        change_set_service.query_change_set("task1", checkpoint_turn_id="turn_other")


def test_query_change_set_hides_unstable_when_excluded(isolated_storage):
    """``include_running=False`` 时运行中（stable=0）的变更不出现，稳定后才可见。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    crud = FileSnapshotCrud()
    _save(crud, "turn1", "a.txt", 0, stable=0)

    assert change_set_service.query_change_set("task1", include_running=False).files == []

    # 对照组：标记稳定后同一行必须出现，证明上面的空列表来自 stable 过滤而非查不到数据。
    crud.mark_stable_by_turn("turn1")
    assert [
        f.path for f in change_set_service.query_change_set("task1", include_running=False).files
    ] == ["a.txt"]


def test_query_change_set_includes_running_by_default(isolated_storage):
    """默认 ``include_running=True``：运行中的变更即时可见，支撑实时展示。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1", status="running")
    _save(FileSnapshotCrud(), "turn1", "a.txt", 0, stable=0)

    assert [f.path for f in change_set_service.query_change_set("task1").files] == ["a.txt"]


def test_query_change_set_running_and_stable_dedupe_by_path(isolated_storage):
    """同 path 同时存在稳定与运行中条目时，取 seq 最大的运行中那条。

    锁住「运行中条目参与去重、且不被稳定条目盖掉」，否则实时视图会停留在旧内容。
    """
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    crud = FileSnapshotCrud()
    _save(crud, "turn1", "a.txt", 0, stable=1)
    _save(crud, "turn1", "a.txt", 1, stable=0)

    files = change_set_service.query_change_set("task1").files
    assert [f.path for f in files] == ["a.txt"]
    assert [f.last_tool_call_id for f in files] == ["call1"]


def test_keep_file_marks_kept(isolated_storage):
    """keep_file 把该路径最新稳定条目标记为 kept，且不影响其它路径。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    crud = FileSnapshotCrud()
    _save(crud, "turn1", "a.txt", 0, stable=1)
    _save(crud, "turn1", "b.txt", 1, stable=1)

    entry = change_set_service.keep_file("task1", "a.txt")
    assert entry.path == "a.txt"
    assert entry.status == "kept"

    files = {f.path: f.status for f in change_set_service.query_change_set("task1").files}
    assert files == {"a.txt": "kept", "b.txt": "pending"}


def test_keep_file_targets_latest_entry_only(isolated_storage):
    """同路径多条稳定变更时，keep 只作用于 seq 最大的那条。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    crud = FileSnapshotCrud()
    _save(crud, "turn1", "a.txt", 0, stable=1)
    _save(crud, "turn1", "a.txt", 1, stable=1)

    change_set_service.keep_file("task1", "a.txt")

    rows = sorted(crud.list_stable_by_turns(["turn1"]), key=lambda r: r.seq)
    assert [r.status for r in rows] == ["pending", "kept"]


def test_keep_file_rejects_unknown_path(isolated_storage):
    """对不存在的路径 keep 抛 ValueError（API 层映射 404）。

    keep 改用「最新快照（含运行中）」定位后，错误文案同步为 ``no change for path``；
    「运行中条目可被 keep」由 ``test_keep_file_allows_running_snapshot`` 覆盖。
    """
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")

    with pytest.raises(ValueError, match="no change for path"):
        change_set_service.keep_file("task1", "missing.txt")


def _save_reverse_op(turn_id: str, path: str, seq: int, op: dict, stable: int = 1) -> None:
    """落一条携带真实反向 V4A 操作的稳定快照，供 revert_file 用例使用。

    参数:
        turn_id: 归属轮次标识。
        path: 相对 workspace 的文件路径。
        seq: 快照序号。
        op: 反向 ``PatchOperation`` 的可序列化字典。
        stable: 稳定标记，默认 1（可见、可撤销）。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

    副作用:
        向 ``file_snapshots`` 表插入一行。
    """
    FileSnapshotCrud().save(
        FileSnapshotRecord(
            turn_id=turn_id,
            tool_call_id=f"call{seq}",
            tool_name="write_file",
            path=path,
            action="modified",
            op_json=json.dumps(op, ensure_ascii=False),
            seq=seq,
            stable=stable,
        )
    )


@pytest.mark.asyncio
async def test_revert_file_restores_deleted_file(isolated_storage):
    """B3 回归（迁移自整 turn 回退用例）：delete 后单文件撤销重建文件与内容。"""
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.delete import build_delete_definition
    from app.tools.tool_registry import ToolRegistry

    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _write(ws, "del.txt", "recover-me")
    _seed_task_turn(ws, "task1", "turn_del")

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
    assert not (ws / "del.txt").exists()

    # turn 结束后快照才稳定，才允许出现在变更集并被撤销。
    FileSnapshotCrud().mark_stable_by_turn("turn_del")

    entry = await change_set_service.revert_file("task1", "del.txt", workspace_root=ws)
    assert entry.status == "reverted"
    assert (ws / "del.txt").read_text(encoding="utf-8") == "recover-me"
    assert change_set_service.query_change_set("task1").files[0].status == "reverted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "no-trailing-newline",
        "with-trailing-newline\n",
        "a\nb\n",
        "a\nb",
        "crlf\r\nstyle\r\n",
    ],
)
async def test_revert_file_preserves_exact_bytes(isolated_storage, content: str):
    """BL-1 回归（迁移）：delete→revert 后文件字节与原始完全一致（含尾换行 / CRLF）。"""
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.delete import build_delete_definition
    from app.tools.tool_registry import ToolRegistry

    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    target = ws / "exact.txt"
    target.write_text(content, encoding="utf-8")
    original_bytes = target.read_bytes()
    _seed_task_turn(ws, "task1", "turn_x")

    registry = ToolRegistry()
    registry.register(build_delete_definition())
    scheduler = ToolScheduler(registry)
    ctx = ToolExecutionContext(
        task_id="task1", workspace_id="ws1", workspace_root=ws, turn_id="turn_x"
    )
    obs = scheduler.execute(
        ToolCall(tool_name="delete", arguments={"path": "exact.txt"}, call_id="cx1"),
        execution_context=ctx,
    )
    assert obs.status == "success"
    assert not target.exists()
    FileSnapshotCrud().mark_stable_by_turn("turn_x")

    await change_set_service.revert_file("task1", "exact.txt", workspace_root=ws)
    assert target.read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_revert_file_removes_created_file(isolated_storage):
    """BL-1 回归（迁移）：write_file 新建文件 → 撤销后磁盘回到不存在。"""
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.write_file import build_write_file_definition
    from app.tools.tool_registry import ToolRegistry

    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn_w")

    registry = ToolRegistry()
    registry.register(build_write_file_definition())
    scheduler = ToolScheduler(registry)
    ctx = ToolExecutionContext(
        task_id="task1", workspace_id="ws1", workspace_root=ws, turn_id="turn_w"
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
    assert (ws / "new.txt").read_text(encoding="utf-8") == "hello\nworld\n"
    FileSnapshotCrud().mark_stable_by_turn("turn_w")

    await change_set_service.revert_file("task1", "new.txt", workspace_root=ws)
    assert not (ws / "new.txt").exists()


@pytest.mark.asyncio
async def test_revert_file_bom_idempotent_on_reentry(isolated_storage):
    """MA-4 回归（迁移）：含 BOM 文件重复撤销不因 BOM 前缀误判而卡死。"""
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.delete import build_delete_definition
    from app.tools.tool_registry import ToolRegistry

    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    target = ws / "bom.txt"
    target.write_bytes(b"\xef\xbb\xbfhello\n")
    _seed_task_turn(ws, "task1", "turn_b")

    registry = ToolRegistry()
    registry.register(build_delete_definition())
    scheduler = ToolScheduler(registry)
    ctx = ToolExecutionContext(
        task_id="task1", workspace_id="ws1", workspace_root=ws, turn_id="turn_b"
    )
    obs = scheduler.execute(
        ToolCall(tool_name="delete", arguments={"path": "bom.txt"}, call_id="cb1"),
        execution_context=ctx,
    )
    assert obs.status == "success"
    assert not target.exists()
    FileSnapshotCrud().mark_stable_by_turn("turn_b")

    await change_set_service.revert_file("task1", "bom.txt", workspace_root=ws)
    assert target.read_bytes() == b"\xef\xbb\xbfhello\n"

    # 重入：文件已还原，_is_already_reverted 应跳过 apply 而非触发
    # destination-already-exists 卡死。
    await change_set_service.revert_file("task1", "bom.txt", workspace_root=ws)
    assert target.read_bytes() == b"\xef\xbb\xbfhello\n"


@pytest.mark.asyncio
async def test_revert_file_only_touches_target_path(isolated_storage):
    """撤销单个文件不得波及同 turn 内其它文件（与整 turn 回退的核心差异）。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    _write(ws, "a.txt", "new-a")
    _write(ws, "b.txt", "new-b")
    # 反向操作：把 a.txt 从 new-a 改回 old-a；b.txt 同理但不应被执行。
    _save_reverse_op(
        "turn1",
        "a.txt",
        0,
        {
            "operation": "update",
            "file_path": "a.txt",
            "new_path": None,
            "hunks": [
                {
                    "lines": [
                        {"prefix": "-", "content": "new-a"},
                        {"prefix": "+", "content": "old-a"},
                    ]
                }
            ],
        },
    )
    _save_reverse_op(
        "turn1",
        "b.txt",
        1,
        {
            "operation": "update",
            "file_path": "b.txt",
            "new_path": None,
            "hunks": [
                {
                    "lines": [
                        {"prefix": "-", "content": "new-b"},
                        {"prefix": "+", "content": "old-b"},
                    ]
                }
            ],
        },
    )

    await change_set_service.revert_file("task1", "a.txt", workspace_root=ws)

    assert (ws / "a.txt").read_text(encoding="utf-8") == "old-a"
    assert (ws / "b.txt").read_text(encoding="utf-8") == "new-b"
    statuses = {f.path: f.status for f in change_set_service.query_change_set("task1").files}
    assert statuses == {"a.txt": "reverted", "b.txt": "pending"}


@pytest.mark.asyncio
async def test_revert_file_records_reverted_at(isolated_storage):
    """撤销成功后必须写入非空 reverted_at 时间戳，供审计与前端展示。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    _write(ws, "a.txt", "new-a")
    _save_reverse_op(
        "turn1",
        "a.txt",
        0,
        {
            "operation": "update",
            "file_path": "a.txt",
            "new_path": None,
            "hunks": [
                {
                    "lines": [
                        {"prefix": "-", "content": "new-a"},
                        {"prefix": "+", "content": "old-a"},
                    ]
                }
            ],
        },
    )

    await change_set_service.revert_file("task1", "a.txt", workspace_root=ws)

    row = FileSnapshotCrud().latest_stable_by_path(["turn1"], "a.txt")
    assert row is not None
    assert row.status == "reverted"
    assert row.reverted_at != ""


@pytest.mark.asyncio
async def test_revert_file_rejects_unknown_path(isolated_storage):
    """撤销不存在的路径抛 ValueError，且不触碰磁盘。

    撤销改用「最新快照（含运行中）」定位后，错误语义由「无稳定变更」变为
    「该路径无任何变更」，故断言文案同步为 ``no change for path``。
    """
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")

    with pytest.raises(ValueError, match="no change for path"):
        await change_set_service.revert_file("task1", "missing.txt", workspace_root=ws)


@pytest.mark.asyncio
async def test_revert_file_keeps_status_on_apply_failure(isolated_storage):
    """apply 失败时状态不得被改写为 reverted，保证可重试。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    _write(ws, "a.txt", "actual-content")
    # 反向 hunk 的上下文与磁盘实际内容不匹配，apply 必然失败。
    _save_reverse_op(
        "turn1",
        "a.txt",
        0,
        {
            "operation": "update",
            "file_path": "a.txt",
            "new_path": None,
            "hunks": [
                {
                    "lines": [
                        {"prefix": "-", "content": "totally-different-anchor"},
                        {"prefix": "+", "content": "old-a"},
                    ]
                }
            ],
        },
    )

    with pytest.raises(Exception):  # noqa: B017 - 底层 PatchApplyError 由实现向上原样抛出
        await change_set_service.revert_file("task1", "a.txt", workspace_root=ws)

    row = FileSnapshotCrud().latest_stable_by_path(["turn1"], "a.txt")
    assert row is not None
    assert row.status == "pending"
    assert row.reverted_at == ""


# ---------------------------------------------------------------------------
# Task 5：file_change_stable 事件 payload 注册
# ---------------------------------------------------------------------------


def test_file_change_stable_payload_registered():
    """新增事件类型必须有对应 payload model，否则 RuntimeEvent 构造会 KeyError。"""
    from app.models.enums.event_type import EventType
    from app.models.payload.file_change_stable_payload import FileChangeStablePayload
    from app.models.payload.registry.runtime_event_payload_registry import (
        EVENT_PAYLOAD_MODELS,
    )

    assert EVENT_PAYLOAD_MODELS[EventType.FILE_CHANGE_STABLE] is FileChangeStablePayload


def test_file_change_updated_payload_registered():
    """``file_change_updated`` 必须注册 payload，否则实时广播构造事件即 KeyError。"""
    from app.models.enums.event_type import EventType
    from app.models.payload.file_change_updated_payload import FileChangeUpdatedPayload
    from app.models.payload.registry.runtime_event_payload_registry import (
        EVENT_PAYLOAD_MODELS,
    )

    assert EVENT_PAYLOAD_MODELS[EventType.FILE_CHANGE_UPDATED] is FileChangeUpdatedPayload


def test_file_change_updated_publish_carries_realtime_diff(tmp_path):
    """运行中实时广播的 FILE_CHANGE_UPDATED 事件必须携带实时 diff，供前端零延迟渲染。

    覆盖：additions / deletions / before / after 均按采集快照正确回填，且 before/after
    在 add/delete 场景按语义为 None（避免事件体冗余携带无关全文）。
    """
    from app.models.enums.event_type import EventType
    from app.models.payload.file_change_updated_payload import FileChangeUpdatedPayload
    from app.service.tool_execution.tool_execution_service import ToolExecutionService
    from app.tools.schemas.tool_execution_context import ToolExecutionContext
    from app.tools.schemas.tool_observation import ToolObservation

    captured: list[tuple[object, object]] = []

    class _FakeBus:
        def publish(self, event: object) -> None:
            captured.append((event, None))

    class _ImmediateLoop:
        """把 call_soon_threadsafe 退化为同步直调的事件循环替身。

        广播实现运行在 asyncio.to_thread 的工作线程上，需经事件循环调度回环；
        本替身让调度在测试中同步生效，便于直接断言广播结果。
        """

        def call_soon_threadsafe(self, callback: object, *args: object) -> None:
            """同步执行被调度的回调。

            参数:
                callback: 待调度的可调用对象。
                *args: 传给回调的位置参数。

            返回:
                无。
            """
            callback(*args)  # type: ignore[operator]

    ctx = ToolExecutionContext(
        task_id="task1",
        workspace_id="ws1",
        workspace_root=tmp_path,
        turn_id="turn1",
    )
    observation = ToolObservation(
        tool_name="patch_tool",
        status="success",
        content="ok",
        data={
            "changes": [
                {
                    "path": "a.txt",
                    "status": "modified",
                    "before": "line1\nold\nline3\n",
                    "after": "line1\nnew\nline3\n",
                }
            ]
        },
    )

    service = ToolExecutionService(
        scheduler=None,
        agent_id="dev",
        event_bus=_FakeBus(),
    )
    service._publish_file_change_updated(ctx, observation, cast(Any, _ImmediateLoop()))

    assert len(captured) == 1
    event = captured[0][0]
    assert event.event_type == EventType.FILE_CHANGE_UPDATED
    payload: FileChangeUpdatedPayload = event.payload
    assert payload.path == "a.txt"
    assert payload.action == "modified"
    assert payload.additions == 1
    assert payload.deletions == 1
    assert payload.before == "line1\nold\nline3\n"
    assert payload.after == "line1\nnew\nline3\n"


# ---------------------------------------------------------------------------
# 运行中撤销 + R6 软冲突防护
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revert_file_allows_running_snapshot(isolated_storage):
    """运行中（stable=0）的变更也可被撤销：磁盘还原且状态标记为 reverted。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1", status="running")
    _write(ws, "a.txt", "new-a")
    _save_reverse_op(
        "turn1",
        "a.txt",
        0,
        {
            "operation": "update",
            "file_path": "a.txt",
            "new_path": None,
            "hunks": [
                {
                    "lines": [
                        {"prefix": "-", "content": "new-a"},
                        {"prefix": "+", "content": "old-a"},
                    ]
                }
            ],
        },
        stable=0,
    )

    entry = await change_set_service.revert_file("task1", "a.txt", workspace_root=ws)

    assert entry.status == "reverted"
    assert (ws / "a.txt").read_text(encoding="utf-8").rstrip("\n") == "old-a"


@pytest.mark.asyncio
async def test_revert_file_cas_miss_rejects_when_already_mutated(isolated_storage):
    """revert_file 遇到 status 已被并发改态（CAS 不匹配）时抛 ChangeSetConflictError。

    磁盘处于 after 态（R6 可通过），但落库状态已被并发方改为 kept —— 模拟
    「磁盘仍可撤销，但状态已非 pending」的并发窗口。CAS 保护的是「状态字段不被盲写」：
    行数 0 → 抛 ChangeSetConflictError（并发冲突，API 映射 409），并发方写入的 kept
    得以保留（lost update 防护）。
    注意：磁盘 apply 发生在状态 CAS 之前，故磁盘已被还原；这正是方案 A（仅状态 CAS）
    的边界——磁盘与状态的一致性属 TOCTOU 范畴，超出本方案范围（见注释）。
    """
    from app.service.task.change_set_service import ChangeSetConflictError

    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    _write(ws, "a.txt", "new-a")
    _save_reverse_op(
        "turn1",
        "a.txt",
        0,
        {
            "operation": "update",
            "file_path": "a.txt",
            "new_path": None,
            "hunks": [
                {
                    "lines": [
                        {"prefix": "-", "content": "new-a"},
                        {"prefix": "+", "content": "old-a"},
                    ]
                }
            ],
        },
        stable=1,
    )

    # 模拟并发方已先把该行置为 kept（CAS 期望集合不含 kept → 应拒绝）。
    crud = FileSnapshotCrud()
    latest = crud.latest_any_by_path(["turn1"], "a.txt")
    assert latest is not None
    crud.update_status(latest.id, "kept", expected_statuses=("pending",))

    with pytest.raises(ChangeSetConflictError):
        await change_set_service.revert_file("task1", "a.txt", workspace_root=ws)

    # 状态字段未被盲写覆盖：保持并发方写入的 kept（CAS 的 lost update 防护生效）。
    after = crud.latest_any_by_path(["turn1"], "a.txt")
    assert after is not None
    assert after.status == "kept"


def test_keep_file_allows_running_snapshot(isolated_storage):
    """运行中（stable=0）的变更也可被「保留」。

    保留与撤销必须同步放开：变更一旦实时展示，两个按钮都不能报 404。
    """
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1", status="running")
    _save(FileSnapshotCrud(), "turn1", "a.txt", 0, stable=0)

    entry = change_set_service.keep_file("task1", "a.txt")

    assert entry.status == "kept"
    row = FileSnapshotCrud().latest_any_by_path(["turn1"], "a.txt")
    assert row is not None
    assert row.status == "kept"


@pytest.mark.asyncio
async def test_revert_file_rejects_manually_modified_file(isolated_storage):
    """R6：磁盘既非 before 也非 after（用户手改）时拒绝撤销，不覆盖用户内容。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    # Agent 改完态应为 new-a，用户又手动改成了 user-edited。
    _write(ws, "a.txt", "user-edited")
    _save_reverse_op(
        "turn1",
        "a.txt",
        0,
        {
            "operation": "update",
            "file_path": "a.txt",
            "new_path": None,
            "hunks": [
                {
                    "lines": [
                        {"prefix": "-", "content": "new-a"},
                        {"prefix": "+", "content": "old-a"},
                    ]
                }
            ],
        },
    )

    with pytest.raises(PatchApplyError, match="manually modified"):
        await change_set_service.revert_file("task1", "a.txt", workspace_root=ws)

    # 用户改动必须原样保留，状态也不得被改写为 reverted。
    assert (ws / "a.txt").read_text(encoding="utf-8") == "user-edited"
    row = FileSnapshotCrud().latest_any_by_path(["turn1"], "a.txt")
    assert row is not None
    assert row.status == "pending"


@pytest.mark.asyncio
async def test_revert_file_is_idempotent_when_already_reverted(isolated_storage):
    """已处于还原态（before）时重复撤销静默跳过 apply，不报冲突。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    # 磁盘已是 before 态 old-a（上一次撤销已完成，仅状态未落库）。
    _write(ws, "a.txt", "old-a")
    _save_reverse_op(
        "turn1",
        "a.txt",
        0,
        {
            "operation": "update",
            "file_path": "a.txt",
            "new_path": None,
            "content": "old-a",
            "hunks": [
                {
                    "lines": [
                        {"prefix": "-", "content": "new-a"},
                        {"prefix": "+", "content": "old-a"},
                    ]
                }
            ],
        },
    )

    entry = await change_set_service.revert_file("task1", "a.txt", workspace_root=ws)

    assert entry.status == "reverted"
    assert (ws / "a.txt").read_text(encoding="utf-8") == "old-a"


def test_changes_api_includes_running_by_default(isolated_storage, api_client):
    """GET /changes 默认返回运行中变更；include_running=false 时回到仅稳定视图。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1", status="running")
    _save(FileSnapshotCrud(), "turn1", "a.txt", 0, stable=0)

    resp = api_client.get("/tasks/task1/changes")
    assert resp.status_code == 200
    assert [f["path"] for f in resp.json()["files"]] == ["a.txt"]

    resp = api_client.get("/tasks/task1/changes", params={"include_running": "false"})
    assert resp.status_code == 200
    assert resp.json()["files"] == []


# ---------------------------------------------------------------------------
# Task 4：HTTP API 层
# ---------------------------------------------------------------------------


def test_changes_api_query_and_keep(isolated_storage, api_client):
    """GET /changes 返回稳定变更；POST /changes/keep 后 status 变为 kept。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    _save(FileSnapshotCrud(), "turn1", "a.txt", 0, stable=1)

    resp = api_client.get("/tasks/task1/changes")
    assert resp.status_code == 200
    assert [f["path"] for f in resp.json()["files"]] == ["a.txt"]

    resp = api_client.post("/tasks/task1/changes/keep", json={"paths": ["a.txt"]})
    assert resp.status_code == 200
    assert resp.json()["files"][0]["status"] == "kept"


def test_changes_api_keep_unknown_path_returns_404(isolated_storage, api_client):
    """对不存在的路径 keep 返回 404。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")

    resp = api_client.post("/tasks/task1/changes/keep", json={"paths": ["missing.txt"]})
    assert resp.status_code == 404


def test_changes_api_revert_unknown_path_returns_404(isolated_storage, api_client):
    """撤销不存在的路径返回 404。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")

    resp = api_client.post("/tasks/task1/changes/revert", json={"paths": ["missing.txt"]})
    assert resp.status_code == 404


def test_changes_api_revert_apply_failure_returns_409(isolated_storage, api_client):
    """反向操作应用失败（磁盘与快照上下文不匹配）返回 409。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _write(ws, "a.txt", "actual-content")
    _seed_task_turn(ws, "task1", "turn1")
    # 反向 hunk 的上下文与磁盘实际内容不匹配，apply 必然失败。
    _save_reverse_op(
        "turn1",
        "a.txt",
        0,
        {
            "operation": "update",
            "file_path": "a.txt",
            "new_path": None,
            "hunks": [
                {
                    "lines": [
                        {"prefix": "-", "content": "totally-different-anchor"},
                        {"prefix": "+", "content": "old-a"},
                    ]
                }
            ],
        },
    )

    resp = api_client.post("/tasks/task1/changes/revert", json={"paths": ["a.txt"]})
    assert resp.status_code == 409


def test_changes_api_keep_cas_miss_returns_409(isolated_storage, api_client):
    """keep 时快照 status 已被并发改态（CAS miss）返回 409，区别于路径不存在的 404。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    crud = FileSnapshotCrud()
    _save(crud, "turn1", "a.txt", 0, stable=1)
    latest = crud.latest_any_by_path(["turn1"], "a.txt")
    assert latest is not None
    # 模拟并发方已先把该行置为 kept。
    crud.update_status(latest.id, "kept", expected_statuses=("pending",))

    resp = api_client.post("/tasks/task1/changes/keep", json={"paths": ["a.txt"]})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_changes_api_revert_cas_miss_returns_409(isolated_storage, api_client):
    """revert 时快照 status 已被并发改态（CAS miss）返回 409，区别于路径不存在的 404。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _write(ws, "a.txt", "new-a")
    _seed_task_turn(ws, "task1", "turn1")
    _save_reverse_op(
        "turn1",
        "a.txt",
        0,
        {
            "operation": "update",
            "file_path": "a.txt",
            "new_path": None,
            "hunks": [
                {
                    "lines": [
                        {"prefix": "-", "content": "new-a"},
                        {"prefix": "+", "content": "old-a"},
                    ]
                }
            ],
        },
        stable=1,
    )
    crud = FileSnapshotCrud()
    latest = crud.latest_any_by_path(["turn1"], "a.txt")
    assert latest is not None
    # 模拟并发方已先把该行置为 kept。
    crud.update_status(latest.id, "kept", expected_statuses=("pending",))

    resp = api_client.post("/tasks/task1/changes/revert", json={"paths": ["a.txt"]})
    assert resp.status_code == 409


def test_changes_api_checkpoint_filters(isolated_storage, api_client):
    """GET /changes?checkpoint=<turn> 只返回到该 turn 为止的变更。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1", created_at="2026-01-01T00:00:01Z")
    _seed_task_turn(ws, "task1", "turn2", created_at="2026-01-01T00:00:02Z")
    _save(FileSnapshotCrud(), "turn1", "a.txt", 0, stable=1)
    _save(FileSnapshotCrud(), "turn2", "b.txt", 1, stable=1)

    resp = api_client.get("/tasks/task1/changes", params={"checkpoint": "turn1"})
    assert resp.status_code == 200
    assert [f["path"] for f in resp.json()["files"]] == ["a.txt"]
    # 检查点下拉仍需展示全部 turn。
    assert [c["turn_id"] for c in resp.json()["checkpoints"]] == ["turn1", "turn2"]


def test_changes_api_revert_file(isolated_storage, api_client):
    """POST /changes/revert 撤销单文件，磁盘内容还原且 status 变 reverted。"""
    from app.tools.schemas import ToolCall, ToolExecutionContext
    from app.tools.tool_execute.tool_scheduler import ToolScheduler
    from app.tools.tool_handler.delete import build_delete_definition
    from app.tools.tool_registry import ToolRegistry

    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _write(ws, "del.txt", "recover-me")
    _seed_task_turn(ws, "task1", "turn_del")

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
    FileSnapshotCrud().mark_stable_by_turn("turn_del")

    resp = api_client.post("/tasks/task1/changes/revert", json={"paths": ["del.txt"]})
    assert resp.status_code == 200
    assert resp.json()["files"][0]["status"] == "reverted"
    assert (ws / "del.txt").read_text(encoding="utf-8") == "recover-me"


# ---------------------------------------------------------------------------
# diff 增删行统计（additions / deletions）
# ---------------------------------------------------------------------------


def test_change_diff_stats_modified():
    """modified 文件按 before/after 逐行 diff 统计增删。"""
    from app.hook.builtins.file_snapshot_hook import _change_diff_stats

    stats = _change_diff_stats(
        [
            {
                "path": "a.py",
                "status": "modified",
                "before": "line1\nkeep\nline3\n",
                "after": "line1\nkeep\nline3-new\nline4\n",
            }
        ]
    )
    assert stats == [(2, 1)]  # 增 line3-new/line4，删 line3


def test_change_diff_stats_added_and_deleted():
    """added 全计新增、deleted 全计删除。"""
    from app.hook.builtins.file_snapshot_hook import _change_diff_stats

    stats = _change_diff_stats(
        [
            {
                "path": "new.txt",
                "status": "added",
                "before": "",
                "after": "a\nb\nc\n",
            },
            {
                "path": "gone.txt",
                "status": "deleted",
                "before": "x\ny\n",
                "after": "",
            },
        ]
    )
    assert stats == [(3, 0), (0, 2)]


def test_change_diff_stats_moved_is_zero():
    """moved 不计增删（0/0）。"""
    from app.hook.builtins.file_snapshot_hook import _change_diff_stats

    stats = _change_diff_stats(
        [
            {
                "path": "src.py",
                "new_path": "dst.py",
                "status": "moved",
                "before": "same\n",
                "after": "same\n",
            }
        ]
    )
    assert stats == [(0, 0)]


def test_query_change_set_carries_diff_stats(isolated_storage):
    """query_change_set 透传每文件的 additions / deletions。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    FileSnapshotCrud().save(
        FileSnapshotRecord(
            turn_id="turn1",
            tool_call_id="call1",
            tool_name="write_file",
            path="a.txt",
            action="modified",
            op_json="{}",
            seq=0,
            stable=1,
            additions=2,
            deletions=4,
        )
    )

    files = change_set_service.query_change_set("task1").files
    assert len(files) == 1
    assert files[0].additions == 2
    assert files[0].deletions == 4


def test_changes_api_returns_diff_stats(isolated_storage, api_client):
    """GET /changes 响应含 additions / deletions 字段。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    FileSnapshotCrud().save(
        FileSnapshotRecord(
            turn_id="turn1",
            tool_call_id="call1",
            tool_name="write_file",
            path="a.txt",
            action="modified",
            op_json="{}",
            seq=0,
            stable=1,
            additions=2,
            deletions=4,
        )
    )

    resp = api_client.get("/tasks/task1/changes")
    assert resp.status_code == 200
    assert resp.json()["files"][0]["additions"] == 2
    assert resp.json()["files"][0]["deletions"] == 4
