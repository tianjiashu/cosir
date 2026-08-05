# Task 级变更集（Changes 面板）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已落地的「整 turn 回退」收敛为「task 级累积变更集 + 单文件保留/撤销」，并在桌面端右侧面板新增「变更」Tab 实时展示。

**Architecture:** 复用现有 `file_snapshots` 表（其 `op_json` 已是反向 V4A 操作）作为变更集底座，新增 `stable` / `status` / `reverted_at` 三列；`turn_revert_service` 收敛为 `change_set_service`，提供 task 级查询与单文件 revert/keep；turn 结束时在 `runner.py` 把该 turn 快照置 `stable=1` 并经 `RuntimeEventBus` 广播 `file_change_stable` 事件；前端新增 `ChangesTab` 组件族 + `useChanges` hook，初始全量拉取 + SSE 增量校准。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2.x / SQLite / pytest；React + TypeScript + Vite / shadcn-ui / Tailwind。

## Global Constraints

- 依赖与命令统一走 `uv`，后端工作目录 `apps/backend`；测试命令 `uv run pytest`。
- 所有新增/修改的 Python 函数必须有中文四段式 docstring（参数 / 返回 / 异常 / 副作用）。
- 日志一律 `from app.config.logging.logger import log`；`event` 为英文 snake_case 字面量，`msg` 中文，业务字段放 `data`。禁止 `print`、禁止空 `except`。
- 绝对导入 `from app.xxx import yyy`；Ruff 行宽 100、双引号；新代码必须带完整类型注解。
- 分层方向：`api → service`，`service → storage/models/tools`，`storage → models`。API 层不得直连 `storage`。
- 主库 schema 演进由 `init_schema._ensure_model_columns` 自动「加列不删列」，**不要手写 ALTER 语句**。
- 变更状态三态字面量固定为 `"pending"` / `"kept"` / `"reverted"`；变更动作固定为 `"created"` / `"modified"` / `"deleted"`。
- 每个 Task 结束时 commit，message 用 `feat:` / `refactor:` / `test:` 前缀。

---

### Task 1: file_snapshots 三列扩展（stable / status / reverted_at）

**Files:**
- Modify: `apps/backend/app/storage/model/file_snapshot_model.py`
- Modify: `apps/backend/app/models/file_snapshot_record.py`（工作区可能已含本任务改动，需核对一致性）
- Test: `apps/backend/tests/test_change_set.py`（本任务新建）

**Interfaces:**
- Consumes: 无（首个任务）。
- Produces:
  - `FileSnapshotModel.stable: Mapped[int]`、`.status: Mapped[str]`、`.reverted_at: Mapped[str]`
  - `FileSnapshotRecord(stable: int = 0, status: str = "pending", reverted_at: str = "")`，`from_model` / `to_row_dict` 覆盖三列。

- [ ] **Step 1: 写失败测试**

新建 `apps/backend/tests/test_change_set.py`。先看 `apps/backend/tests/test_turn_revert.py` 顶部的 `isolated_storage` fixture 用法并沿用（由同一 conftest 提供）。

```python
"""task 级变更集（change_set_service）测试。"""

from app.models.file_snapshot_record import FileSnapshotRecord
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud


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
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd apps/backend && uv run pytest tests/test_change_set.py -v
```
Expected: FAIL，`TypeError: __init__() got an unexpected keyword argument 'stable'` 或 `AttributeError: 'FileSnapshotModel' object has no attribute 'stable'`。

- [ ] **Step 3: 给 ORM 模型加三列**

在 `file_snapshot_model.py` 的 `seq` 列之后追加：

```python
    # 变更是否已稳定：所属 turn 结束时置 1，运行中落库为 0（运行中不展示、不可撤销）。
    stable: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 用户对该文件最新变更的处理态：pending / kept / reverted。
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    # 撤销时间（仅 status=reverted 时有值），用于排查；非 reverted 时为空字符串。
    reverted_at: Mapped[str] = mapped_column(Text, nullable=False, default="")
```

同时把类 docstring 补一句说明 `stable` 与 `status` 语义。

- [ ] **Step 4: 核对值对象字段**

确认 `file_snapshot_record.py` 的 `FileSnapshotRecord` 含 `stable: int = 0`、`status: str = "pending"`、`reverted_at: str = ""`，且 `from_model` 读取这三列、`to_row_dict` 写出这三列。若工作区已有该改动则跳过编辑。

- [ ] **Step 5: 运行测试确认通过**

```bash
cd apps/backend && uv run pytest tests/test_change_set.py -v
```
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add apps/backend/app/storage/model/file_snapshot_model.py apps/backend/app/models/file_snapshot_record.py apps/backend/tests/test_change_set.py
git commit -m "feat: file_snapshots 增加 stable/status/reverted_at 列"
```

---

### Task 2: FileSnapshotCrud 变更集查询与状态更新方法

**Files:**
- Modify: `apps/backend/app/storage/crud/file_snapshot_crud.py`
- Test: `apps/backend/tests/test_change_set.py`

**Interfaces:**
- Consumes: Task 1 的 `FileSnapshotRecord.stable / status / reverted_at`。
- Produces（`FileSnapshotCrud` 新增方法）：
  - `list_stable_by_turns(turn_ids: list[str]) -> list[FileSnapshotRecord]`：给定 turn 集合下 `stable == 1` 的记录，按 `seq` 升序。
  - `latest_stable_by_path(turn_ids: list[str], path: str) -> FileSnapshotRecord | None`：该 path 在集合内 `seq` 最大的稳定行。
  - `mark_stable_by_turn(turn_id: str) -> int`：把该 turn 尚未稳定的行置 `stable = 1`，返回受影响行数。
  - `update_status(snapshot_id: int, status: str, reverted_at: str = "") -> None`：按主键更新状态。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_change_set.py`：

```python
def _save(crud, turn_id, path, seq, stable=0):
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
    """mark_stable_by_turn 后可查到稳定行，latest_stable_by_path 取 seq 最大者。"""
    crud = FileSnapshotCrud()
    _save(crud, "turn1", "a.txt", 0)
    _save(crud, "turn1", "a.txt", 1)
    _save(crud, "turn2", "b.txt", 2)

    assert crud.mark_stable_by_turn("turn1") == 2
    stable_rows = crud.list_stable_by_turns(["turn1", "turn2"])
    assert [r.path for r in stable_rows] == ["a.txt", "a.txt"]

    latest = crud.latest_stable_by_path(["turn1"], "a.txt")
    assert latest is not None
    assert latest.seq == 1

    crud.update_status(latest.id, "reverted", reverted_at="2026-08-04T00:00:00")
    refreshed = crud.latest_stable_by_path(["turn1"], "a.txt")
    assert refreshed is not None
    assert refreshed.status == "reverted"
    assert refreshed.reverted_at == "2026-08-04T00:00:00"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd apps/backend && uv run pytest tests/test_change_set.py::test_mark_stable_and_query_latest -v
```
Expected: FAIL，`AttributeError: 'FileSnapshotCrud' object has no attribute 'mark_stable_by_turn'`。

- [ ] **Step 3: 实现四个 CRUD 方法**

把 `file_snapshot_crud.py` 顶部 import 改为 `from sqlalchemy import delete, func, select, update`，在 `clear_by_turn` 之前追加：

```python
    def list_stable_by_turns(self, turn_ids: list[str]) -> list[FileSnapshotRecord]:
        """按 turn 集合查询全部已稳定快照，按 ``seq`` 升序。

        升序返回是为了让调用方按顺序覆盖同 path 条目，天然得到「每个 path 的最新变更」。

        参数:
            turn_ids: 目标轮次标识列表；为空列表时直接返回空结果，不查库。

        返回:
            ``stable == 1`` 的 ``FileSnapshotRecord`` 列表，按 ``seq`` 升序；无记录时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        if not turn_ids:
            return []
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(FileSnapshotModel)
                    .where(FileSnapshotModel.turn_id.in_(turn_ids))
                    .where(FileSnapshotModel.stable == 1)
                    .order_by(FileSnapshotModel.seq.asc())
                )
                .scalars()
                .all()
            )
        return [FileSnapshotRecord.from_model(row) for row in rows]

    def latest_stable_by_path(self, turn_ids: list[str], path: str) -> FileSnapshotRecord | None:
        """取给定 turn 集合内某文件路径的最新已稳定快照。

        参数:
            turn_ids: 目标轮次标识列表；为空列表时返回 None。
            path: 相对 workspace 的文件路径。

        返回:
            ``seq`` 最大的已稳定 ``FileSnapshotRecord``；无匹配时为 None。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        if not turn_ids:
            return None
        with self._session_factory() as session:
            row = (
                session.execute(
                    select(FileSnapshotModel)
                    .where(FileSnapshotModel.turn_id.in_(turn_ids))
                    .where(FileSnapshotModel.path == path)
                    .where(FileSnapshotModel.stable == 1)
                    .order_by(FileSnapshotModel.seq.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
        return None if row is None else FileSnapshotRecord.from_model(row)

    def mark_stable_by_turn(self, turn_id: str) -> int:
        """把某 turn 的全部快照标记为已稳定（turn 结束时调用，幂等）。

        参数:
            turn_id: 目标轮次标识。

        返回:
            本次实际被更新的行数（已稳定的行不重复计入，故重复调用返回 0）。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            把 ``file_snapshots`` 中该 turn 尚未稳定的行 ``stable`` 置 1。
        """
        with self._session_factory.begin() as session:
            result = session.execute(
                update(FileSnapshotModel)
                .where(FileSnapshotModel.turn_id == turn_id)
                .where(FileSnapshotModel.stable == 0)
                .values(stable=1)
            )
        return int(result.rowcount or 0)

    def update_status(self, snapshot_id: int, status: str, reverted_at: str = "") -> None:
        """按主键更新单条快照的处理态。

        参数:
            snapshot_id: 快照主键。
            status: 目标状态，取值 ``pending`` / ``kept`` / ``reverted``。
            reverted_at: 撤销时间字符串；仅 ``status == "reverted"`` 时有意义，其余传空串。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            改写 ``file_snapshots`` 中一行的 ``status`` 与 ``reverted_at``。
        """
        with self._session_factory.begin() as session:
            session.execute(
                update(FileSnapshotModel)
                .where(FileSnapshotModel.id == snapshot_id)
                .values(status=status, reverted_at=reverted_at)
            )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd apps/backend && uv run pytest tests/test_change_set.py -v
```
Expected: PASS（2 passed）。

- [ ] **Step 5: Commit**

```bash
git add apps/backend/app/storage/crud/file_snapshot_crud.py apps/backend/tests/test_change_set.py
git commit -m "feat: FileSnapshotCrud 增加变更集查询与状态更新方法"
```

---

### Task 3: change_set_service（由 turn_revert_service 收敛）

**Files:**
- Rename: `apps/backend/app/service/task/turn_revert_service.py` → `apps/backend/app/service/task/change_set_service.py`（用 `git mv` 保留历史）
- Test: `apps/backend/tests/test_change_set.py`
- Delete: `apps/backend/tests/test_turn_revert.py`（整 turn 回退用例随 `revert_turn` 移除；文件级还原回归用例迁入 `test_change_set.py`）

**Interfaces:**
- Consumes: Task 2 的 `FileSnapshotCrud.list_stable_by_turns / latest_stable_by_path / update_status`。
- Produces（`app.service.task.change_set_service` 模块级）：
  - `@dataclass(frozen=True) class ChangeFileEntry`：`path: str`、`action: str`、`status: str`、`last_tool_call_id: str`、`last_turn_id: str`
  - `@dataclass(frozen=True) class ChangeCheckpoint`：`turn_id: str`、`turn_seq: int`、`label: str`
  - `@dataclass(frozen=True) class ChangeSet`：`task_id: str`、`checkpoints: list[ChangeCheckpoint]`、`files: list[ChangeFileEntry]`
  - `def query_change_set(task_id: str, checkpoint_turn_id: str | None = None) -> ChangeSet`
  - `def keep_file(task_id: str, path: str) -> ChangeFileEntry`
  - `async def revert_file(task_id: str, path: str, workspace_root: Path | None = None) -> ChangeFileEntry`
  - 保留复用：`_snapshots_to_operations`、`_is_already_reverted`、`_hunk_to_content_patch`、`_hunk_to_content_patch_impl`、`_resolve_workspace_root`
- 删除：`RevertResult`、`revert_turn`、`_revert_turn_locked`、`_clear_turn_state`、`_restore_files`、`_is_latest_finished_turn`、`_collect_non_revertible`、`_acquire_lock`、`_revert_locks`、`_revert_locks_guard`、`_FINISHED_STATUSES`、`_NON_REVERTIBLE_TOOLS`，以及随之无用的 import（`asyncio`、`AsyncSqliteSaver`、`TurnMessageCrud`、`RuntimeEventCrud`、`checkpoint_path`、`new_event_id`、`TurnStatus`）。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_change_set.py`。`_seed_task_turn` 从 `tests/test_turn_revert.py` 复制同名 helper 并简化（不需要传 status 时可默认 completed）：

```python
import pytest

from app.service.task import change_set_service


def test_query_change_set_dedupes_by_path(isolated_storage):
    """同一路径多次变更只输出最新一条，且按 turn 生成检查点。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    _seed_task_turn(ws, "task1", "turn2")
    crud = FileSnapshotCrud()
    _save(crud, "turn1", "a.txt", 0, stable=1)
    _save(crud, "turn2", "a.txt", 1, stable=1)
    _save(crud, "turn2", "b.txt", 2, stable=1)

    change_set = change_set_service.query_change_set("task1")
    assert [f.path for f in change_set.files] == ["a.txt", "b.txt"]
    assert [f.last_turn_id for f in change_set.files] == ["turn2", "turn2"]
    assert [c.turn_id for c in change_set.checkpoints] == ["turn1", "turn2"]


def test_query_change_set_respects_checkpoint(isolated_storage):
    """指定检查点时只返回到该 turn（含）为止的累积变更。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    _seed_task_turn(ws, "task1", "turn2")
    crud = FileSnapshotCrud()
    _save(crud, "turn1", "a.txt", 0, stable=1)
    _save(crud, "turn2", "b.txt", 1, stable=1)

    change_set = change_set_service.query_change_set("task1", checkpoint_turn_id="turn1")
    assert [f.path for f in change_set.files] == ["a.txt"]


def test_query_change_set_hides_unstable(isolated_storage):
    """运行中（stable=0）的变更不出现在变更集里。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    _save(FileSnapshotCrud(), "turn1", "a.txt", 0, stable=0)

    assert change_set_service.query_change_set("task1").files == []


def test_keep_file_marks_kept(isolated_storage):
    """keep_file 把该路径最新稳定条目标记为 kept。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")
    _save(FileSnapshotCrud(), "turn1", "a.txt", 0, stable=1)

    entry = change_set_service.keep_file("task1", "a.txt")
    assert entry.status == "kept"
    assert change_set_service.query_change_set("task1").files[0].status == "kept"


def test_keep_file_rejects_unknown_path(isolated_storage):
    """对不存在的路径 keep 抛 ValueError（API 层映射 404）。"""
    ws = isolated_storage["tmp_path"] / "ws"
    ws.mkdir()
    _seed_task_turn(ws, "task1", "turn1")

    with pytest.raises(ValueError, match="no stable change"):
        change_set_service.keep_file("task1", "missing.txt")
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd apps/backend && uv run pytest tests/test_change_set.py -v
```
Expected: FAIL，`ModuleNotFoundError: No module named 'app.service.task.change_set_service'`。

- [ ] **Step 3: 重命名文件**

```bash
git mv apps/backend/app/service/task/turn_revert_service.py apps/backend/app/service/task/change_set_service.py
```

- [ ] **Step 4: 删除整 turn 回退逻辑并改写模块 docstring**

删除上文 Interfaces 中「删除」列出的全部符号与无用 import。模块 docstring 改为：

```python
"""Task 级变更集服务（累积文件变更查询 + 单文件保留/撤销）。

单一职责：把 ``file_snapshots`` 中已稳定的反向 V4A 快照聚合成 task 维度的变更集，
并提供单文件「撤销」（把文件还原到该变更之前）与「保留」（标记已确认）两个操作入口。
不直接写 SQL、不承载模型，仅编排 storage 与工具应用逻辑。

设计约束：
- 只暴露 ``stable == 1`` 的变更：运行中的工具调用尚未定稿，不可展示、不可撤销。
- 同一路径多次变更按 ``seq`` 取最新一条对外呈现；撤销即对该最新条目的反向操作 apply。
- 检查点粒度为「一个 turn = 一个检查点」，用于查看「到某次对话为止」的累积变更。
"""
```

- [ ] **Step 5: 实现值对象与入口函数**

顶部 import 补 `from datetime import UTC, datetime`，然后追加：

```python
@dataclass(frozen=True)
class ChangeFileEntry:
    """变更集中的单个文件条目。

    参数:
        path: 相对 workspace 的文件路径。
        action: 变更动作，取值 ``created`` / ``modified`` / ``deleted``。
        status: 用户处理态，取值 ``pending`` / ``kept`` / ``reverted``。
        last_tool_call_id: 产生该最新变更的工具调用标识。
        last_turn_id: 产生该最新变更的轮次标识。
    """

    path: str
    action: str
    status: str
    last_tool_call_id: str
    last_turn_id: str


@dataclass(frozen=True)
class ChangeCheckpoint:
    """变更集检查点（一个 turn 对应一个检查点）。

    参数:
        turn_id: 轮次标识。
        turn_seq: 该 turn 在 task 内的顺序号，从 1 开始。
        label: 展示用标签，如 ``检查点 1``。
    """

    turn_id: str
    turn_seq: int
    label: str


@dataclass(frozen=True)
class ChangeSet:
    """某 task 的累积变更集。

    参数:
        task_id: 任务标识。
        checkpoints: 该 task 下按时间升序的检查点列表。
        files: 按路径去重后的文件条目列表（每个路径保留最新一条变更）。
    """

    task_id: str
    checkpoints: list[ChangeCheckpoint]
    files: list[ChangeFileEntry]


def _turn_ids_until(
    task_id: str, checkpoint_turn_id: str | None
) -> tuple[list[str], list[ChangeCheckpoint]]:
    """解析 task 下参与聚合的 turn 列表与检查点列表。

    参数:
        task_id: 任务标识。
        checkpoint_turn_id: 检查点轮次标识；为 None 表示取全部 turn。

    返回:
        二元组 ``(turn_ids, checkpoints)``：前者为参与变更聚合的 turn 标识（按时间升序，
        指定检查点时截断到该 turn 含），后者为该 task 全部检查点（不受截断影响，供前端下拉）。

    异常:
        ValueError: 当 ``checkpoint_turn_id`` 不属于该 task 时抛出。

    副作用:
        打开主库只读查询。
    """
    turns = TurnCrud().list_by_task(task_id)
    checkpoints = [
        ChangeCheckpoint(turn_id=turn.turn_id, turn_seq=index, label=f"检查点 {index}")
        for index, turn in enumerate(turns, start=1)
    ]
    turn_ids = [turn.turn_id for turn in turns]
    if checkpoint_turn_id is not None:
        if checkpoint_turn_id not in turn_ids:
            raise ValueError(f"checkpoint turn not in task: {checkpoint_turn_id}")
        turn_ids = turn_ids[: turn_ids.index(checkpoint_turn_id) + 1]
    return turn_ids, checkpoints


def query_change_set(task_id: str, checkpoint_turn_id: str | None = None) -> ChangeSet:
    """查询某 task 的累积文件变更集（仅已稳定条目）。

    同一路径多次变更按 ``seq`` 升序覆盖，最终只保留最新一条对外呈现。

    参数:
        task_id: 任务标识。
        checkpoint_turn_id: 只聚合到该 turn（含）为止的变更；为 None 表示全部。

    返回:
        ``ChangeSet``：含检查点列表与按路径去重的文件条目（按路径字典序排列）。

    异常:
        ValueError: 当 ``checkpoint_turn_id`` 不属于该 task 时抛出。

    副作用:
        打开主库只读查询。
    """
    turn_ids, checkpoints = _turn_ids_until(task_id, checkpoint_turn_id)
    latest: dict[str, ChangeFileEntry] = {}
    for snap in FileSnapshotCrud().list_stable_by_turns(turn_ids):
        latest[snap.path] = ChangeFileEntry(
            path=snap.path,
            action=snap.action,
            status=snap.status,
            last_tool_call_id=snap.tool_call_id,
            last_turn_id=snap.turn_id,
        )
    return ChangeSet(
        task_id=task_id,
        checkpoints=checkpoints,
        files=[latest[path] for path in sorted(latest)],
    )


def _require_latest_stable(task_id: str, path: str) -> FileSnapshotRecord:
    """取某 task 下指定路径的最新已稳定快照，缺失时报错。

    参数:
        task_id: 任务标识。
        path: 相对 workspace 的文件路径。

    返回:
        该路径最新的已稳定 ``FileSnapshotRecord``。

    异常:
        ValueError: 当该路径没有已稳定变更时抛出（API 层映射为 404）。

    副作用:
        打开主库只读查询。
    """
    turn_ids, _ = _turn_ids_until(task_id, None)
    snapshot = FileSnapshotCrud().latest_stable_by_path(turn_ids, path)
    if snapshot is None:
        raise ValueError(f"no stable change for path: {path}")
    return snapshot


def keep_file(task_id: str, path: str) -> ChangeFileEntry:
    """把某文件的最新变更标记为「保留」。

    仅改写展示态，不触碰磁盘。

    参数:
        task_id: 任务标识。
        path: 相对 workspace 的文件路径。

    返回:
        更新后的 ``ChangeFileEntry``（``status == "kept"``）。

    异常:
        ValueError: 当该路径没有已稳定变更时抛出。

    副作用:
        改写 ``file_snapshots`` 中一行的 ``status``；写一条 info 日志。
    """
    snapshot = _require_latest_stable(task_id, path)
    FileSnapshotCrud().update_status(snapshot.id, "kept")
    log.info(
        "change_set_file_kept",
        extra={
            "msg": "变更集：文件变更已标记为保留",
            "data": {"task_id": task_id, "path": path, "turn_id": snapshot.turn_id},
        },
    )
    return ChangeFileEntry(
        path=snapshot.path,
        action=snapshot.action,
        status="kept",
        last_tool_call_id=snapshot.tool_call_id,
        last_turn_id=snapshot.turn_id,
    )


async def revert_file(
    task_id: str, path: str, workspace_root: Path | None = None
) -> ChangeFileEntry:
    """撤销某文件的最新变更，把文件还原到该变更之前。

    应用 ``op_json`` 中的反向 V4A 操作；若磁盘已处于还原后状态（重复撤销），
    跳过 apply 并直接标记，保证幂等。

    参数:
        task_id: 任务标识。
        path: 相对 workspace 的文件路径。
        workspace_root: 调用方注入的 workspace 根路径；为 None 时经 task→workspace 解析。

    返回:
        更新后的 ``ChangeFileEntry``（``status == "reverted"``）。

    异常:
        ValueError: 当该路径没有已稳定变更时抛出。
        PatchApplyError: 当反向操作应用失败时向上抛出，此时状态不被改写，可重试。

    副作用:
        修改 workspace 内文件；改写 ``file_snapshots`` 的 ``status`` 与 ``reverted_at``；写日志。
    """
    snapshot = _require_latest_stable(task_id, path)
    if workspace_root is None:
        workspace_root = _resolve_workspace_root(task_id)
    resolver = ProjectPathResolver(workspace_root)
    for operation in _snapshots_to_operations([snapshot]):
        if _is_already_reverted(operation, resolver):
            continue
        try:
            apply_all_with_diff([operation], resolver)
        except Exception:
            log.exception(
                "change_set_revert_failed",
                extra={
                    "msg": "变更集：单文件撤销失败",
                    "data": {
                        "task_id": task_id,
                        "path": path,
                        "turn_id": snapshot.turn_id,
                        "operation": operation.operation.value,
                    },
                },
            )
            raise
    FileSnapshotCrud().update_status(
        snapshot.id, "reverted", reverted_at=datetime.now(UTC).isoformat()
    )
    log.info(
        "change_set_file_reverted",
        extra={
            "msg": "变更集：文件变更已撤销",
            "data": {"task_id": task_id, "path": path, "turn_id": snapshot.turn_id},
        },
    )
    return ChangeFileEntry(
        path=snapshot.path,
        action=snapshot.action,
        status="reverted",
        last_tool_call_id=snapshot.tool_call_id,
        last_turn_id=snapshot.turn_id,
    )
```

- [ ] **Step 6: 运行测试确认通过**

```bash
cd apps/backend && uv run pytest tests/test_change_set.py -v
```
Expected: 全部 PASS。

- [ ] **Step 7: 迁移可复用回归用例并删除旧测试文件**

从 `tests/test_turn_revert.py` 把以下**文件级还原**用例迁入 `tests/test_change_set.py`：把 `await revert_turn("turnX", workspace_root=ws)` 改为 `await change_set_service.revert_file("task1", "<path>", workspace_root=ws)`，并在 seed 快照后调用 `FileSnapshotCrud().mark_stable_by_turn("<turn_id>")` 使快照可见：
- `test_delete_then_revert_recreates_file`
- `test_delete_revert_preserves_exact_bytes`
- `test_write_revert_preserves_trailing_newline`
- `utf-8-sig` BOM 重入用例

然后删除旧文件：

```bash
git rm apps/backend/tests/test_turn_revert.py
```

- [ ] **Step 8: 运行本文件全量测试**

```bash
cd apps/backend && uv run pytest tests/test_change_set.py -q
```
Expected: 全绿。（此时 `turns_api.py` 仍 import `revert_turn`，全仓 pytest 会收集失败，属预期，Task 4 修复。）

- [ ] **Step 9: Commit**

```bash
git add -A apps/backend/app/service/task apps/backend/tests
git commit -m "refactor: turn_revert_service 收敛为 task 级 change_set_service"
```

---

### Task 4: API 端点（删除 rollback，新增 changes / revert / keep）

**Files:**
- Modify: `apps/backend/app/api/turns_api.py`（删除 `revert_turn` import 与 `POST /turns/{turn_id}/rollback`）
- Create: `apps/backend/app/api/changes_api.py`
- Modify: `apps/backend/app/api/app.py`（注册新路由模块）
- Create: `apps/backend/app/api/schemas/request/ChangeSetActionRequest.py`
- Create: `apps/backend/app/api/schemas/response/ChangeSetResponse.py`
- Test: `apps/backend/tests/test_change_set.py`

**Interfaces:**
- Consumes: Task 3 的 `change_set_service.query_change_set / keep_file / revert_file` 与三个值对象。
- Produces（HTTP 契约）：
  - `GET /tasks/{task_id}/changes?checkpoint=<turn_id>` → `ChangeSetResponse`
  - `POST /tasks/{task_id}/changes/keep`，body `{"paths": ["a.txt"]}` → `ChangeSetResponse`
  - `POST /tasks/{task_id}/changes/revert`，body `{"paths": ["a.txt"]}` → `ChangeSetResponse`

  单文件与批量共用同一端点（`paths` 数组长度为 1 即单文件），不再单列 `revert-batch` / `keep-batch`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_change_set.py`。TestClient 用 `fastapi.testclient.TestClient` 包裹 `app.api.app` 的应用实例（先读 `app/api/app.py` 确认是 `create_app()` 工厂还是模块级 `app` 单例，照真实形态构造）：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd apps/backend && uv run pytest tests/test_change_set.py -k changes_api -v
```
Expected: FAIL（路由未注册返回 404，或 `api_client` fixture 缺失）。

- [ ] **Step 3: 定义请求/响应模型**

`apps/backend/app/api/schemas/request/ChangeSetActionRequest.py`（文件名沿用该目录既有 PascalCase 风格）：

```python
"""变更集操作请求模型（撤销 / 保留共用）。"""

from pydantic import BaseModel, Field


class ChangeSetActionRequest(BaseModel):
    """对一批文件执行撤销或保留的请求体。

    参数:
        paths: 相对 workspace 的文件路径列表，至少一项；长度为 1 即单文件操作。
    """

    paths: list[str] = Field(min_length=1)
```

`apps/backend/app/api/schemas/response/ChangeSetResponse.py`：

```python
"""变更集响应模型。"""

from pydantic import BaseModel

from app.service.task.change_set_service import ChangeSet


class ChangeCheckpointResponse(BaseModel):
    """检查点响应项。"""

    turn_id: str
    turn_seq: int
    label: str


class ChangeFileResponse(BaseModel):
    """文件变更响应项。"""

    path: str
    action: str
    status: str
    last_tool_call_id: str
    last_turn_id: str


class ChangeSetResponse(BaseModel):
    """某 task 的累积变更集响应。"""

    task_id: str
    checkpoints: list[ChangeCheckpointResponse]
    files: list[ChangeFileResponse]

    @classmethod
    def from_change_set(cls, change_set: ChangeSet) -> "ChangeSetResponse":
        """从服务层值对象构造响应模型。

        参数:
            change_set: 服务层返回的 ``ChangeSet``。

        返回:
            对应的 ``ChangeSetResponse``。

        异常:
            无。

        副作用:
            无。
        """
        return cls(
            task_id=change_set.task_id,
            checkpoints=[
                ChangeCheckpointResponse(turn_id=c.turn_id, turn_seq=c.turn_seq, label=c.label)
                for c in change_set.checkpoints
            ],
            files=[
                ChangeFileResponse(
                    path=f.path,
                    action=f.action,
                    status=f.status,
                    last_tool_call_id=f.last_tool_call_id,
                    last_turn_id=f.last_turn_id,
                )
                for f in change_set.files
            ],
        )
```

- [ ] **Step 4: 新建 changes_api.py**

先读 `turns_api.py` 顶部确认 FastAPI `app` 对象的取得方式，照同一模式实现：

```python
"""Task 级变更集 API（查询累积变更、单/多文件撤销与保留）。

单一职责：把 ``change_set_service`` 的能力暴露为 HTTP 端点，只做入参校验、
异常到状态码的映射与响应投影，不承载业务规则。
"""

from fastapi import HTTPException, Query

from app.api.app import app
from app.api.schemas.request.ChangeSetActionRequest import ChangeSetActionRequest
from app.api.schemas.response.ChangeSetResponse import ChangeSetResponse
from app.service.task import change_set_service
from app.tools.tool_handler.patch.patch_apply import PatchApplyError


@app.get("/tasks/{task_id}/changes", response_model=ChangeSetResponse)
async def get_changes(
    task_id: str, checkpoint: str | None = Query(default=None)
) -> ChangeSetResponse:
    """查询某 task 的累积文件变更集。

    参数:
        task_id: 任务标识。
        checkpoint: 可选检查点 turn 标识，只返回到该 turn（含）为止的累积变更。

    返回:
        ``ChangeSetResponse``。

    异常:
        HTTPException(404): 当 checkpoint 不属于该 task 时。

    副作用:
        只读查询。
    """
    try:
        return ChangeSetResponse.from_change_set(
            change_set_service.query_change_set(task_id, checkpoint)
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/tasks/{task_id}/changes/keep", response_model=ChangeSetResponse)
async def keep_changes(task_id: str, request: ChangeSetActionRequest) -> ChangeSetResponse:
    """把一批文件的最新变更标记为「保留」。

    参数:
        task_id: 任务标识。
        request: 含 ``paths`` 的请求体。

    返回:
        操作后的完整 ``ChangeSetResponse``，供前端直接替换本地状态。

    异常:
        HTTPException(404): 当任一路径没有已稳定变更时。

    副作用:
        改写 ``file_snapshots`` 的 status。
    """
    try:
        for path in request.paths:
            change_set_service.keep_file(task_id, path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ChangeSetResponse.from_change_set(change_set_service.query_change_set(task_id))


@app.post("/tasks/{task_id}/changes/revert", response_model=ChangeSetResponse)
async def revert_changes(task_id: str, request: ChangeSetActionRequest) -> ChangeSetResponse:
    """撤销一批文件的最新变更，把它们还原到变更之前。

    参数:
        task_id: 任务标识。
        request: 含 ``paths`` 的请求体。

    返回:
        操作后的完整 ``ChangeSetResponse``。

    异常:
        HTTPException(404): 当任一路径没有已稳定变更时。
        HTTPException(409): 当反向操作应用失败（磁盘已被外部改动等）时。

    副作用:
        修改 workspace 内文件；改写 ``file_snapshots`` 的 status 与 reverted_at。
    """
    try:
        for path in request.paths:
            await change_set_service.revert_file(task_id, path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PatchApplyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ChangeSetResponse.from_change_set(change_set_service.query_change_set(task_id))
```

- [ ] **Step 5: 注册路由并删除 rollback**

在 `app/api/app.py` 中找到用 `importlib.import_module("app.api.xxx")` 触发注册的位置，追加 `importlib.import_module("app.api.changes_api")`。**必须用 importlib，不能写 `import app.api.changes_api`**，否则顶层包名 `app` 会被覆盖为模块对象。

在 `turns_api.py` 删除 `from app.service.task.turn_revert_service import revert_turn`（约第 65 行），并删除自 `@app.post("/turns/{turn_id}/rollback")`（约第 209 行）起的整个 `rollback_turn` 端点。删除后检查 `PatchApplyError` 等 import 是否仍有其他使用者，无则一并删除。

- [ ] **Step 6: 运行测试确认通过**

```bash
cd apps/backend && uv run pytest tests/test_change_set.py -v
```
Expected: 全部 PASS。

- [ ] **Step 7: 全量测试 + 静态检查**

```bash
cd apps/backend && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy app
```
Expected: 全绿。若其他测试文件残留对 `revert_turn` / `rollback` 的引用，一并清理。

- [ ] **Step 8: Commit**

```bash
git add -A apps/backend/app/api apps/backend/tests
git commit -m "feat: 新增 task 级变更集 API 并移除 turn rollback 端点"
```

---

### Task 5: turn 结束置 stable + 广播 file_change_stable 事件

**Files:**
- Create: `apps/backend/app/models/payload/file_change_stable_payload.py`
- Modify: `apps/backend/app/models/payload/__init__.py`（re-export 新 payload）
- Modify: `apps/backend/app/models/payload/registry/runtime_event_payload_registry.py`（注册映射）
- Modify: `apps/backend/app/models/enums/event_type.py`（新增 `FILE_CHANGE_STABLE`）
- Modify: `apps/backend/app/core/runtime/runner.py`（turn 正常结束路径）
- Test: `apps/backend/tests/test_change_set.py`

**Interfaces:**
- Consumes: Task 2 的 `FileSnapshotCrud.mark_stable_by_turn / list_stable_by_turns`。
- Produces:
  - `EventType.FILE_CHANGE_STABLE = "file_change_stable"`
  - `FileChangeStablePayload(task_id: str, turn_id: str, path: str, action: str)`
  - `runner.py` 私有协程 `async def _publish_stable_file_changes(self, task_id: str, turn_id: str) -> None`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_change_set.py`：

```python
def test_file_change_stable_payload_registered():
    """新增事件类型必须有对应 payload model，否则 RuntimeEvent 构造会 KeyError。"""
    from app.models.enums.event_type import EventType
    from app.models.payload.file_change_stable_payload import FileChangeStablePayload
    from app.models.payload.registry.runtime_event_payload_registry import (
        EVENT_PAYLOAD_MODELS,
    )

    assert EVENT_PAYLOAD_MODELS[EventType.FILE_CHANGE_STABLE] is FileChangeStablePayload
```

> 若 registry 中字典名不是 `EVENT_PAYLOAD_MODELS`，以 `runtime_event_payload_registry.py` 中真实名称为准，同步改测试与实现。

- [ ] **Step 2: 运行测试确认失败**

```bash
cd apps/backend && uv run pytest tests/test_change_set.py -k payload_registered -v
```
Expected: FAIL，`AttributeError: FILE_CHANGE_STABLE`。

- [ ] **Step 3: 新增 payload 与事件类型**

先读 `app/models/payload/run_started_payload.py` 作为格式样板（基类、字段定义方式、docstring 风格），照其形态新建 `file_change_stable_payload.py`：

```python
"""``file_change_stable`` 事件 payload 值对象。"""

from dataclasses import dataclass

from app.models.payload.runtime_event_payload import RuntimeEventPayload


@dataclass(frozen=True)
class FileChangeStablePayload(RuntimeEventPayload):
    """某文件变更随所属 turn 结束而稳定，可展示与撤销。

    参数:
        task_id: 变更所属任务标识。
        turn_id: 产生该变更的轮次标识。
        path: 相对 workspace 的文件路径。
        action: 变更动作，取值 ``created`` / ``modified`` / ``deleted``。
    """

    task_id: str
    turn_id: str
    path: str
    action: str
```

**务必以 `run_started_payload.py` 的真实基类与定义方式为准**：若它不是 dataclass 而是 pydantic model，或基类名不同，照搬其形态，不要套用本示例。

然后：
- `event_type.py` 枚举追加 `FILE_CHANGE_STABLE = "file_change_stable"`；
- `payload/__init__.py` 追加 `from .file_change_stable_payload import FileChangeStablePayload` 及 `__all__` 条目（照该文件既有写法）；
- `registry/runtime_event_payload_registry.py` 的映射字典追加 `EventType.FILE_CHANGE_STABLE: FileChangeStablePayload`。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd apps/backend && uv run pytest tests/test_change_set.py -k payload_registered -v
```
Expected: PASS。

- [ ] **Step 5: 在 runner 中置 stable 并广播**

在 `app/core/runtime/runner.py` 的 `run_turn` 正常完成路径（`_persist_turn_trajectory` 调用之后、返回之前）插入：

```python
        await self._publish_stable_file_changes(task_id, turn_id)
```

变量名以该处上下文真实可用的为准。新增私有方法（放在 `_persist_turn_trajectory` 附近，保持相关代码相邻）：

```python
    async def _publish_stable_file_changes(self, task_id: str, turn_id: str) -> None:
        """把本 turn 的文件快照标记为已稳定，并逐条广播 file_change_stable 事件。

        turn 结束意味着其内所有工具调用已定稿，此时变更才对用户可见、可撤销。

        参数:
            task_id: 所属任务标识。
            turn_id: 刚结束的轮次标识。

        返回:
            无。

        异常:
            无。变更集广播属展示侧增强，任何失败都不应让已成功的 turn 被判为失败，
            故整体捕获并记 warning；前端仍可靠 ``GET /tasks/{id}/changes`` 全量校准。

        副作用:
            把该 turn 的 file_snapshots 行置 stable=1；向事件总线发布若干事件。
        """
        try:
            crud = FileSnapshotCrud()
            if crud.mark_stable_by_turn(turn_id) == 0:
                return
            for snapshot in crud.list_stable_by_turns([turn_id]):
                await self._event_bus.publish(
                    RuntimeEvent(
                        event_type=EventType.FILE_CHANGE_STABLE,
                        task_id=task_id,
                        turn_id=turn_id,
                        payload=FileChangeStablePayload(
                            task_id=task_id,
                            turn_id=turn_id,
                            path=snapshot.path,
                            action=snapshot.action,
                        ),
                    )
                )
        except Exception:
            log.warning(
                "file_change_stable_publish_failed",
                extra={
                    "msg": "变更集稳定标记或事件广播失败，不影响 turn 结果",
                    "data": {"task_id": task_id, "turn_id": turn_id},
                },
            )
```

**`RuntimeEvent(...)` 的构造参数（是否需要 `event_id` / `sequence` / `created_at`）与 `self._event_bus.publish(...)` 的真实签名，必须先读 `runner.py` 中已有的事件发布调用点照搬，不要凭示例臆造。** 顶部补 import：`FileSnapshotCrud`、`FileChangeStablePayload`（`EventType` / `RuntimeEvent` / `log` 通常已有）。

- [ ] **Step 6: 运行全量测试与静态检查**

```bash
cd apps/backend && uv run pytest -q && uv run ruff check . && uv run mypy app
```
Expected: 全绿。

- [ ] **Step 7: Commit**

```bash
git add -A apps/backend/app
git commit -m "feat: turn 结束时标记变更稳定并广播 file_change_stable 事件"
```

---

### Task 6: 前端 API 层与 useChanges hook

**Files:**
- Modify: `apps/shared/ts/api.ts`（新增 3 个路径常量）
- Modify: `apps/desktop/src/services/api.ts`（新增类型与 3 个请求函数）
- Create: `apps/desktop/src/hooks/useChanges.ts`
- Modify: `apps/desktop/src/services/sse.ts`（仅当事件类型是封闭联合时需扩展）

**Interfaces:**
- Consumes: Task 4 的 HTTP 契约、Task 5 的 `file_change_stable` SSE 事件。
- Produces:
  - 类型 `ChangeFile { path, action, status, last_tool_call_id, last_turn_id }`
  - 类型 `ChangeCheckpoint { turn_id, turn_seq, label }`
  - 类型 `ChangeSet { task_id, checkpoints, files }`
  - `fetchChangeSet(taskId: string, checkpoint?: string): Promise<ChangeSet>`
  - `revertChanges(taskId: string, paths: string[]): Promise<ChangeSet>`
  - `keepChanges(taskId: string, paths: string[]): Promise<ChangeSet>`
  - `useChanges(taskId: string | null)` → `{ changeSet, checkpoint, setCheckpoint, revert, keep, refresh, loading, error }`

- [ ] **Step 1: 加路径常量**

先读 `apps/shared/ts/api.ts` 中 `API_PATHS` 的既有写法，照同一风格追加：

```ts
  taskChanges: (taskId: string) => `/tasks/${taskId}/changes`,
  taskChangesRevert: (taskId: string) => `/tasks/${taskId}/changes/revert`,
  taskChangesKeep: (taskId: string) => `/tasks/${taskId}/changes/keep`,
```

- [ ] **Step 2: 加类型与请求函数**

在 `apps/desktop/src/services/api.ts` 追加类型：

```ts
export interface ChangeFile {
  path: string;
  action: string;
  status: 'pending' | 'kept' | 'reverted';
  last_tool_call_id: string;
  last_turn_id: string;
}

export interface ChangeCheckpoint {
  turn_id: string;
  turn_seq: number;
  label: string;
}

export interface ChangeSet {
  task_id: string;
  checkpoints: ChangeCheckpoint[];
  files: ChangeFile[];
}
```

再照该文件已有请求函数的写法（同一 `request` / `http` 封装与错误处理）实现三个函数：`fetchChangeSet` GET `taskChanges`（`checkpoint` 非空时拼 `?checkpoint=`）、`revertChanges` POST `taskChangesRevert`、`keepChanges` POST `taskChangesKeep`（body `{ paths }`），三者均返回 `ChangeSet`。

- [ ] **Step 3: 实现 useChanges hook**

新建 `apps/desktop/src/hooks/useChanges.ts`：

- 状态：`changeSet: ChangeSet | null`、`checkpoint: string | null`、`loading: boolean`、`error: string | null`。
- `refresh`：调 `fetchChangeSet(taskId, checkpoint ?? undefined)`，整体替换 `changeSet`；异常写入 `error`。
- `useEffect` 依赖 `[taskId, checkpoint]` 触发 `refresh`；`taskId` 为 null 时清空状态不发请求。
- `revert(paths)` / `keep(paths)`：调对应 API，用返回的完整 `ChangeSet` 替换本地状态（服务端已返回全量，无需乐观更新与二次拉取）。
- SSE 增量：订阅 `file_change_stable`，事件 `payload.task_id === taskId` 时调 `refresh()`（全量校准，避免在前端重复实现检查点过滤与按 path 去重逻辑）。
- 卸载时取消订阅。

- [ ] **Step 4: 接入 SSE 事件类型**

读 `apps/desktop/src/services/sse.ts`：若事件类型是封闭联合，追加 `'file_change_stable'` 与 payload 类型 `{ task_id: string; turn_id: string; path: string; action: string }`；若已是开放 `string`，无需改动。

- [ ] **Step 5: 类型检查**

```bash
cd apps/desktop && npm run build
```
Expected: 构建通过、无 TS 报错。若有独立 `typecheck` script 则优先用它。

- [ ] **Step 6: Commit**

```bash
git add apps/shared/ts/api.ts apps/desktop/src/services apps/desktop/src/hooks/useChanges.ts
git commit -m "feat: 前端接入 task 级变更集 API 与 useChanges hook"
```

---

### Task 7: ChangesTab 组件族与 RightPanel 接入

**Files:**
- Create: `apps/desktop/src/components/right-panel/ChangesTab.tsx`
- Create: `apps/desktop/src/components/right-panel/ChangeFileRow.tsx`
- Create: `apps/desktop/src/components/right-panel/ChangeCheckpointSelect.tsx`
- Create: `apps/desktop/src/components/right-panel/ChangesToolbar.tsx`
- Modify: `apps/desktop/src/components/layout/RightPanel.tsx`

**Interfaces:**
- Consumes: Task 6 的 `useChanges`、`ChangeFile`、`ChangeCheckpoint`。
- Produces:
  - `<ChangesTab taskId={string} />`
  - `<ChangeFileRow file={ChangeFile} selected={boolean} onToggleSelect={(path: string) => void} onKeep={(path: string) => void} onRevert={(path: string) => void} />`
  - `<ChangeCheckpointSelect checkpoints={ChangeCheckpoint[]} value={string | null} onChange={(turnId: string | null) => void} />`
  - `<ChangesToolbar selectedCount={number} onKeepSelected={() => void} onRevertSelected={() => void} onRefresh={() => void} />`

- [ ] **Step 1: 实现 ChangeFileRow**

单行三段式布局：
- 左：checkbox + 文件路径（`truncate` + `title` 显示全路径）。
- 中：action 标签，`created→新建`、`modified→修改`、`deleted→删除`，用 `components/ui` 下已有的 Badge 组件。
- 右：「保留」「撤销」两个按钮。

状态样式：`status === 'reverted'` 整行降透明度、两按钮禁用；`status === 'kept'`「保留」按钮呈已选中态。Tailwind 类名与配色照 `right-panel` 目录已有组件的习惯，不自创色板。

- [ ] **Step 2: 实现 ChangeCheckpointSelect 与 ChangesToolbar**

`ChangeCheckpointSelect`：用 `components/ui` 下已有的 Select 组件，选项为「最新」（value `null`）加各 `checkpoint.label`，`onChange` 回传 `turn_id`。

`ChangesToolbar`：左侧显示 `已选 N 项`，右侧三个按钮「保留选中」「撤销选中」「刷新」；`selectedCount === 0` 时前两个禁用。

- [ ] **Step 3: 实现 ChangesTab**

组合以上组件：
- 调 `useChanges(taskId)` 取数据与操作。
- 本地 `selected: Set<string>` 管理多选；每次 `changeSet` 替换后清空选中。
- 顶部渲染 `ChangeCheckpointSelect`（`checkpoints` 来自 `changeSet`）与 `ChangesToolbar`。
- 列表渲染 `changeSet.files.map(...)` → `ChangeFileRow`。
- 空态：`files.length === 0` 时显示「暂无文件变更」占位，照 `right-panel` 其他 Tab 的空态写法。
- `loading` 时列表区显示骨架或加载提示；`error` 非空时显示错误条。

- [ ] **Step 4: RightPanel 接入**

在 `apps/desktop/src/components/layout/RightPanel.tsx` 的 tab 列表（现有 `Outputs` / `Sources`）中追加「变更」项，选中时渲染 `<ChangesTab taskId={当前 taskId} />`。taskId 从该组件既有的 props 或 store 获取，照文件内现有取值方式。

- [ ] **Step 5: 构建验证**

```bash
cd apps/desktop && npm run build
```
Expected: 构建通过。

- [ ] **Step 6: 手动冒烟**

启动后端与桌面端，在一个已有文件改动的 task 中打开「变更」Tab，验证：
1. 列表展示已稳定变更；
2. 切换检查点后列表按预期收窄；
3. 点「保留」行标记为 kept；
4. 点「撤销」后磁盘文件确实回到改动前，行标记为 reverted；
5. 新一轮对话结束后列表自动出现新变更（SSE 生效）。

- [ ] **Step 7: Commit**

```bash
git add apps/desktop/src/components
git commit -m "feat: 右侧面板新增变更 Tab 与文件级保留/撤销交互"
```

---

## 附：清理确认清单

实现完成后确认以下项已无残留：

- [ ] 全仓无 `turn_revert_service` 引用（`rg turn_revert` 无结果，`tool_execution_context.py` 与 `v4a_reverse.py` 的 docstring 中的旧名一并改为 `change_set_service`）。
- [ ] 全仓无 `POST /turns/{turn_id}/rollback` 引用（含前端 `api.ts` 与 `API_PATHS`）。
- [ ] `docs/turn回退方案.md` 顶部补一段说明：该方案已收敛为 task 级变更集，实施以本计划为准。
