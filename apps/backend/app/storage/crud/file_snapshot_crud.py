"""``file_snapshots`` 表的纯 CRUD 数据访问层（Turn 回退文件快照）。

单一职责：只提供 ``file_snapshots`` 单表的读写，不承担运行时编排。
所有方法通过共享的主库 session 工厂访问数据库，必须在 ``init_storage`` 之后实例化。
"""

import json
from collections.abc import Sequence
from contextlib import nullcontext

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models.file_snapshot_record import FileSnapshotRecord
from app.storage.model.file_snapshot_model import FileSnapshotModel
from app.storage.store_engines import main_session_factory
from app.storage.task_file_sequence import task_file_sequence_lock


class FileSnapshotCrud:
    """``file_snapshots`` 表的纯 CRUD。

    仅负责单表读写与 model↔``FileSnapshotRecord`` 转换，不依赖 service 层。

    并发约定：``save_batch_with_sequence`` 在进程级 task 序号锁内分配并保存连续
    snapshot 序号。文件 mutation 会先保存 ``prepared`` 行以预留序号，再用同一批行
    收口实际结果。
    """

    _sequence_lock = task_file_sequence_lock
    """共享进程锁：串行化 task 内 snapshot 序号分配。"""

    def __init__(self) -> None:
        """绑定主库共享 session 工厂。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 ``init_storage`` 尚未调用（主库 session 工厂不可用）。

        副作用:
            无（仅复用已初始化的主库 session 工厂）。
        """
        self._session_factory = main_session_factory()

    def save(self, record: FileSnapshotRecord) -> None:
        """写入一条文件快照记录。

        参数:
            record: 待落库的 ``FileSnapshotRecord``。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``file_snapshots`` 表插入一行。
        """
        with self._session_factory.begin() as session:
            session.add(FileSnapshotModel(**record.to_row_dict()))

    def save_batch_with_sequence(
        self,
        task_id: int,
        records: list[FileSnapshotRecord],
        *,
        seq_start: int | None = None,
        session: Session | None = None,
    ) -> list[FileSnapshotRecord]:
        """为同一 task 的一批快照分配连续 seq 并批量落库。

        同一 task 下并行工具调用会并发准备快照。本方法把基准计算和批量写入放在
        同一临界区，保证不同 operation 预留的 seq 区间不重叠。
        外部事务必须使用调用方已预留的 ``seq_start``，因此不在持有该事务时等待序号锁。

        参数:
            task_id: 快照归属任务（seq 命名空间边界）。
            records: 待落库的一批快照值对象；它们的 ``seq`` 字段被忽略并以
                ``base + offset``（offset 为在列表内的序号）重写。
            seq_start: 调用方已预留的连续区间起点；提供外部 ``session`` 时必填。
            session: 可选的调用方事务；提供时加入该事务且不自行提交。

        返回:
            分配好实际 ``seq`` 后的 ``FileSnapshotRecord`` 列表（顺序与入参一致）。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果事务执行失败。
            RuntimeError: 如果锁内查询/写入失败（仅理论上）。
            ValueError: 提供外部 ``session`` 但未提供预留的 ``seq_start``。

        副作用:
            向 ``file_snapshots`` 表批量插入 ``len(records)`` 行；空列表时不操作。
        """
        if not records:
            return []
        if session is not None and seq_start is None:
            raise ValueError("seq_start is required when saving snapshots in an external session")
        sequence_guard = self._sequence_lock if session is None else nullcontext()
        with sequence_guard:
            base = self._next_seq_for_task(task_id) if seq_start is None else seq_start
            assert base is not None
            for offset, record in enumerate(records):
                object.__setattr__(
                    record,
                    "seq",
                    base + offset,
                )
            if session is not None:
                rows = [FileSnapshotModel(**record.to_row_dict()) for record in records]
                session.add_all(rows)
                session.flush()
            else:
                with self._session_factory.begin() as owned_session:
                    rows = [FileSnapshotModel(**record.to_row_dict()) for record in records]
                    owned_session.add_all(rows)
                    owned_session.flush()
            for record, row in zip(records, rows, strict=True):
                object.__setattr__(record, "id", row.id)
        return records

    def _next_seq_for_task(self, task_id: int) -> int:
        """返回该 task 当前 ``MAX(seq)+1`` 作为新一批快照的 seq 基准。

        调用方必须在持有 ``_sequence_lock`` 时调用，保证基准在批量插入完成前不被
        其它调用改写；本方法自身不参与锁管理。

        参数:
            task_id: 目标任务标识（seq 命名空间边界）。

        返回:
            下一个可用 seq 基准；该 task 尚无快照时返回 0。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        with self._session_factory() as session:
            max_seq = session.execute(
                select(func.max(FileSnapshotModel.seq)).where(FileSnapshotModel.task_id == task_id)
            ).scalar()
        return 0 if max_seq is None else int(max_seq) + 1

    def list_unfinalized(self) -> list[FileSnapshotRecord]:
        """Return durable prepared file mutations and interrupted ChangeSet reverts."""

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(FileSnapshotModel)
                    .where(FileSnapshotModel.mutation_state.in_(("prepared", "reverting")))
                    .order_by(FileSnapshotModel.task_id, FileSnapshotModel.seq)
                )
                .scalars()
                .all()
            )
        return [FileSnapshotRecord.from_model(row) for row in rows]

    def finalize_prepared(
        self,
        mutation_id: str,
        records: list[FileSnapshotRecord],
    ) -> list[FileSnapshotRecord]:
        """Replace prepared per-path rows with the observed ChangeSet, or remove no-ops.

        Prepared rows reserve task sequence slots before file effects begin. Final records
        reuse those slots and primary keys so concurrent operations keep start-order
        sequencing. A mutation cannot produce more final records than its prepared path
        manifest; violating that invariant is a caller error.
        """

        with self._session_factory.begin() as session:
            rows = (
                session.execute(
                    select(FileSnapshotModel)
                    .where(FileSnapshotModel.mutation_id == mutation_id)
                    .where(FileSnapshotModel.mutation_state == "prepared")
                    .order_by(FileSnapshotModel.seq)
                )
                .scalars()
                .all()
            )
            if len(records) > len(rows):
                raise ValueError("final ChangeSet exceeds its prepared snapshot slots")
            finalized: list[FileSnapshotRecord] = []
            for row, record in zip(rows, records, strict=False):
                values = record.to_row_dict()
                values["seq"] = row.seq
                for field, value in values.items():
                    setattr(row, field, value)
                row.mutation_id = ""
                row.mutation_state = "applied"
                row.mutation_json = ""
                finalized.append(FileSnapshotRecord.from_model(row))
            for row in rows[len(records) :]:
                session.delete(row)
        return finalized

    def delete_prepared(self, mutation_id: str) -> int:
        """Delete a prepared mutation that made no workspace change."""

        with self._session_factory.begin() as session:
            result = session.execute(
                delete(FileSnapshotModel)
                .where(FileSnapshotModel.mutation_id == mutation_id)
                .where(FileSnapshotModel.mutation_state == "prepared")
            )
        return int(result.rowcount or 0)

    def begin_revert(
        self,
        snapshot_ids: Sequence[int],
        mutation_id: str,
        *,
        session: Session,
    ) -> int:
        """Mark a pending ChangeSet group as being reverted before touching its files."""

        if not snapshot_ids:
            return 0
        result = session.execute(
            update(FileSnapshotModel)
            .where(FileSnapshotModel.id.in_(snapshot_ids))
            .where(FileSnapshotModel.status == "pending")
            .where(FileSnapshotModel.mutation_state == "applied")
            .values(
                mutation_id=mutation_id,
                mutation_state="reverting",
            )
        )
        return int(result.rowcount or 0)

    def update_mutation_records(
        self,
        mutation_id: str,
        updates: Sequence[dict[str, object]],
    ) -> int:
        """Finalize an interrupted revert by updating its snapshot rows atomically.

        Each update contains ``id`` plus selected snapshot columns. All rows must still
        belong to the same reverting mutation; otherwise the operation is rejected.
        """

        with self._session_factory.begin() as session:
            rows = (
                session.execute(
                    select(FileSnapshotModel)
                    .where(FileSnapshotModel.mutation_id == mutation_id)
                    .where(FileSnapshotModel.mutation_state == "reverting")
                )
                .scalars()
                .all()
            )
            row_by_id = {row.id: row for row in rows}
            if any(update_row.get("id") not in row_by_id for update_row in updates):
                raise RuntimeError("ChangeSet revert snapshot group changed during finalization")
            for update_row in updates:
                snapshot_id = update_row["id"]
                if not isinstance(snapshot_id, int):
                    raise ValueError("ChangeSet snapshot update requires an integer id")
                row = row_by_id[snapshot_id]
                for field, value in update_row.items():
                    if field != "id":
                        if field == "op_json" and isinstance(value, dict):
                            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                        setattr(row, field, value)
                if update_row.get("status") == "reverted":
                    envelope = json.loads(row.op_json)
                    for entry in envelope.get("path_states", []):
                        before = entry.get("before") if isinstance(entry, dict) else None
                        if isinstance(before, dict):
                            before.pop("restore_ref", None)
                    row.op_json = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
                row.mutation_id = ""
                row.mutation_state = "applied"
                row.mutation_json = ""
        return len(updates)

    def list_by_turn(self, run_id: int) -> list[FileSnapshotRecord]:
        """按 turn 查询全部快照，按 ``seq`` 降序（回退时逆序应用）。

        参数:
            run_id: 目标轮次标识。

        返回:
            ``FileSnapshotRecord`` 列表，``seq`` 从大到小排列；无记录时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(FileSnapshotModel)
                    .where(FileSnapshotModel.run_id == run_id)
                    .where(FileSnapshotModel.mutation_state == "applied")
                    .order_by(FileSnapshotModel.seq.desc())
                )
                .scalars()
                .all()
            )
        return [FileSnapshotRecord.from_model(row) for row in rows]

    def list_any_by_task(
        self, task_id: int, run_ids: list[int] | None = None
    ) -> list[FileSnapshotRecord]:
        """按 task 查询全部已收口快照，包括所属 Run 仍在运行的记录。

        升序返回，使调用方按顺序覆盖同 path 条目，得到「每个 path 的最新变更」，
        无论该变更是否随 turn 结束稳定。用于运行中可撤销查询。``run_ids`` 仅作
        可选子过滤（如 checkpoint 截断），为 ``None`` 时查询该 task 全部快照。

        参数:
            task_id: 目标任务标识（seq 命名空间边界）。
            run_ids: 可选轮次标识子集；为 ``None`` 时不过滤，为空列表时返回空结果。

        返回:
            ``mutation_state == "applied"`` 的 ``FileSnapshotRecord`` 列表，按 ``seq``
            升序排列；无记录时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        if run_ids is not None and not run_ids:
            return []
        with self._session_factory() as session:
            stmt = (
                select(FileSnapshotModel)
                .where(FileSnapshotModel.task_id == task_id)
                .where(FileSnapshotModel.mutation_state == "applied")
            )
            if run_ids is not None:
                stmt = stmt.where(FileSnapshotModel.run_id.in_(run_ids))
            rows = session.execute(stmt.order_by(FileSnapshotModel.seq.asc())).scalars().all()
        return [FileSnapshotRecord.from_model(row) for row in rows]

    def latest_any_by_path(
        self, task_id: int, path: str, run_ids: list[int] | None = None
    ) -> FileSnapshotRecord | None:
        """取给定 task（可选 turn 子集）内某文件路径的最新快照。

        运行中的 Run 也可能已产生可撤销变更，因此查询不按 Run 终态过滤。

        参数:
            task_id: 目标任务标识（seq 命名空间边界）。
            path: 相对 workspace 的文件路径。
            run_ids: 可选轮次标识子集；为 ``None`` 时不过滤，为空列表时返回 None。

        返回:
            ``seq`` 最大的 ``FileSnapshotRecord``；无匹配时为 None。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        if run_ids is not None and not run_ids:
            return None
        with self._session_factory() as session:
            stmt = (
                select(FileSnapshotModel)
                .where(FileSnapshotModel.task_id == task_id)
                .where(FileSnapshotModel.path == path)
                .where(FileSnapshotModel.mutation_state == "applied")
            )
            if run_ids is not None:
                stmt = stmt.where(FileSnapshotModel.run_id.in_(run_ids))
            row = (
                session.execute(stmt.order_by(FileSnapshotModel.seq.desc()).limit(1))
                .scalars()
                .first()
            )
        return None if row is None else FileSnapshotRecord.from_model(row)

    def update_status(
        self,
        snapshot_id: int,
        status: str,
        expected_statuses: Sequence[str] | None = None,
    ) -> int:
        """按主键更新单条快照的处理态（可选 CAS：仅当当前 status 属于期望集合才更新）。

        该方法把「读取处理态 → 判断 → 写入」收敛为单条带条件的原子 UPDATE，配合 SQLite
        单写者语义，杜绝并发下 ChangeSet 处理态（Keep/Revert）的 lost update：
        - 传入 ``expected_statuses`` 时，SQL 追加 ``AND status IN (...)``，仅当该行当前
          status 未被并发改写才生效；返回 0 表示已被并发改态（调用方应拒绝而非盲写）。
        - 不传 ``expected_statuses`` 时保持无条件更新的旧语义（用于收口等不关心状态的路径）。

        参数:
            snapshot_id: 快照主键。
            status: 目标状态，取值 ``pending`` / ``kept`` / ``reverted``。
            expected_statuses: 可选 CAS 期望的当前状态集合；仅当该行当前 ``status`` 属于此
                集合时更新。为 ``None`` 时不带条件（无条件覆盖）。

        返回:
            本次实际被更新的行数；0 表示 CAS 不匹配（status 已被并发改态）或行不存在。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            改写 ``file_snapshots`` 中一行的 ``status``。
        """
        with self._session_factory.begin() as session:
            stmt = (
                update(FileSnapshotModel)
                .where(FileSnapshotModel.id == snapshot_id)
                .values(status=status)
            )
            if expected_statuses is not None:
                stmt = stmt.where(FileSnapshotModel.status.in_(expected_statuses))
            result = session.execute(stmt)
        return int(result.rowcount or 0)

    def update_statuses(
        self,
        snapshot_ids: Sequence[int],
        status: str,
        *,
        expected_status: str = "pending",
        session: Session,
    ) -> int:
        """CAS-update a snapshot group and release its no-longer-needed before-image refs."""

        if not snapshot_ids:
            return 0
        candidates = session.execute(
            select(FileSnapshotModel.id)
            .where(FileSnapshotModel.id.in_(snapshot_ids))
            .where(FileSnapshotModel.status == expected_status)
        ).scalars()
        candidate_ids = list(candidates)
        if not candidate_ids:
            return 0
        result = session.execute(
            update(FileSnapshotModel)
            .where(FileSnapshotModel.id.in_(candidate_ids))
            .where(FileSnapshotModel.status == expected_status)
            .values(
                status=status,
                mutation_id="",
                mutation_state="applied",
                mutation_json="",
            )
        )
        rows = session.execute(
            select(FileSnapshotModel.id, FileSnapshotModel.op_json)
            .where(FileSnapshotModel.id.in_(candidate_ids))
            .where(FileSnapshotModel.status == status)
        ).all()
        for row in rows:
            try:
                envelope = json.loads(row.op_json)
            except (TypeError, json.JSONDecodeError):
                continue
            path_states = envelope.get("path_states")
            if not isinstance(path_states, list):
                continue
            changed = False
            for entry in path_states:
                before = entry.get("before") if isinstance(entry, dict) else None
                if isinstance(before, dict) and "restore_ref" in before:
                    before.pop("restore_ref", None)
                    changed = True
            if changed:
                session.execute(
                    update(FileSnapshotModel)
                    .where(FileSnapshotModel.id == row.id)
                    .where(FileSnapshotModel.status == status)
                    .values(op_json=json.dumps(envelope, ensure_ascii=False, sort_keys=True))
                )
        return int(result.rowcount or 0)

    def list_pending_op_json(self, session: Session) -> list[str]:
        """Return pending snapshot envelopes for reference counting in a caller transaction."""

        rows = session.execute(
            select(FileSnapshotModel.op_json).where(FileSnapshotModel.status == "pending")
        ).scalars()
        return list(rows)

    def list_unfinalized_mutation_json(self, session: Session) -> list[str]:
        """返回仍待对账的预写操作清单。撤销恢复只依赖快照行和当前文件状态。"""

        rows = session.execute(
            select(FileSnapshotModel.mutation_json).where(
                FileSnapshotModel.mutation_state == "prepared"
            )
        ).scalars()
        return [value for value in rows if value]

    def clear_by_turn(self, run_id: int) -> None:
        """删除某 turn 的全部快照记录。

        参数:
            run_id: 待清理快照的轮次标识。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``file_snapshots`` 表删除匹配 run_id 的行。
        """
        with self._session_factory.begin() as session:
            session.execute(delete(FileSnapshotModel).where(FileSnapshotModel.run_id == run_id))

    def delete_by_task_ids(self, task_ids: list[int], session: Session | None = None) -> None:
        """按一批 task 的标识批量删除其全部文件快照记录。

        与 ``clear_by_turn`` 不同，本方法按 ``task_id`` 整删某任务下的快照（不依赖 run），
        供任务级联删除在外部事务内复用。

        参数:
            task_ids: 待清理的 task 整数 id 列表。
            session: 可选外部事务 session；传入时复用该事务不自行提交，为 None 时
                自开事务并自动提交。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``file_snapshots`` 表删除 ``task_id`` 命中的行；task_ids 为空或对应行
            不存在时静默无操作。
        """

        if not task_ids:
            return
        if session is not None:
            session.execute(
                delete(FileSnapshotModel).where(FileSnapshotModel.task_id.in_(task_ids))
            )
            return
        with self._session_factory.begin() as session:
            session.execute(
                delete(FileSnapshotModel).where(FileSnapshotModel.task_id.in_(task_ids))
            )
