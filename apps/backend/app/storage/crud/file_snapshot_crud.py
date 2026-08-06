"""``file_snapshots`` 表的纯 CRUD 数据访问层（Turn 回退文件快照）。

单一职责：只提供 ``file_snapshots`` 单表的读写，不承担运行时编排。
所有方法通过共享的主库 session 工厂访问数据库，必须在 ``init_storage`` 之后实例化。
"""

from collections.abc import Sequence

from sqlalchemy import delete, func, select, update

from app.models.file_snapshot_record import FileSnapshotRecord
from app.storage.model.file_snapshot_model import FileSnapshotModel
from app.storage.store_engines import main_session_factory


class FileSnapshotCrud:
    """``file_snapshots`` 表的纯 CRUD。

    仅负责单表读写与 model↔``FileSnapshotRecord`` 转换，不依赖 service 层。
    """

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

    def list_by_turn(self, turn_id: str) -> list[FileSnapshotRecord]:
        """按 turn 查询全部快照，按 ``seq`` 降序（回退时逆序应用）。

        参数:
            turn_id: 目标轮次标识。

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
                    .where(FileSnapshotModel.turn_id == turn_id)
                    .order_by(FileSnapshotModel.seq.desc())
                )
                .scalars()
                .all()
            )
        return [FileSnapshotRecord.from_model(row) for row in rows]

    def next_seq(self, turn_id: str) -> int:
        """返回该 turn 下一个可用的全局递增快照序号（MAX(seq)+1）。

        参数:
            turn_id: 目标轮次标识。

        返回:
            下一个 seq；若该 turn 尚无快照则返回 0。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        with self._session_factory() as session:
            max_seq = session.execute(
                select(func.max(FileSnapshotModel.seq)).where(FileSnapshotModel.turn_id == turn_id)
            ).scalar()
        return 0 if max_seq is None else int(max_seq) + 1

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

    def list_any_by_turns(self, turn_ids: list[str]) -> list[FileSnapshotRecord]:
        """按 turn 集合查询全部快照（含运行中 ``stable=0`` 与已稳定 ``stable=1``）。

        升序返回，使调用方按顺序覆盖同 path 条目，得到「每个 path 的最新变更」，
        无论该变更是否随 turn 结束稳定。用于运行中可撤销查询。

        参数:
            turn_ids: 目标轮次标识列表；为空列表时直接返回空结果，不查库。

        返回:
            所有 ``stable`` 取值的 ``FileSnapshotRecord`` 列表，按 ``seq`` 升序；无记录时为空列表。

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

    def latest_any_by_path(self, turn_ids: list[str], path: str) -> FileSnapshotRecord | None:
        """取给定 turn 集合内某文件路径的最新快照（含运行中 ``stable=0``）。

        用于运行中可撤销：运行时同 path 的变更可能尚未稳定，但已是该 path 的
        待撤销目标态，必须纳入查询，否则运行中撤销会漏掉最新一条。

        参数:
            turn_ids: 目标轮次标识列表；为空列表时返回 None。
            path: 相对 workspace 的文件路径。

        返回:
            ``seq`` 最大的 ``FileSnapshotRecord``（不限 stable）；无匹配时为 None。

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

    def update_status(
        self,
        snapshot_id: int,
        status: str,
        reverted_at: str = "",
        expected_statuses: Sequence[str] | None = None,
    ) -> int:
        """按主键更新单条快照的处理态（可选 CAS：仅当当前 status 属于期望集合才更新）。

        该方法把「读取处理态 → 判断 → 写入」收敛为单条带条件的原子 UPDATE，配合 SQLite
        单写者语义，杜绝并发下 `keep_file` / `revert_file` 的 lost update：
        - 传入 ``expected_statuses`` 时，SQL 追加 ``AND status IN (...)``，仅当该行当前
          status 未被并发改写才生效；返回 0 表示已被并发改态（调用方应拒绝而非盲写）。
        - 不传 ``expected_statuses`` 时保持无条件更新的旧语义（用于收口等不关心状态的路径）。

        参数:
            snapshot_id: 快照主键。
            status: 目标状态，取值 ``pending`` / ``kept`` / ``reverted``。
            reverted_at: 撤销时间字符串；仅 ``status == "reverted"`` 时有意义，其余传空串。
            expected_statuses: 可选 CAS 期望的当前状态集合；仅当该行当前 ``status`` 属于此
                集合时更新。为 ``None`` 时不带条件（无条件覆盖）。

        返回:
            本次实际被更新的行数；0 表示 CAS 不匹配（status 已被并发改态）或行不存在。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            改写 ``file_snapshots`` 中一行的 ``status`` 与 ``reverted_at``。
        """
        with self._session_factory.begin() as session:
            stmt = (
                update(FileSnapshotModel)
                .where(FileSnapshotModel.id == snapshot_id)
                .values(status=status, reverted_at=reverted_at)
            )
            if expected_statuses is not None:
                stmt = stmt.where(FileSnapshotModel.status.in_(expected_statuses))
            result = session.execute(stmt)
        return int(result.rowcount or 0)

    def clear_by_turn(self, turn_id: str) -> None:
        """删除某 turn 的全部快照记录。

        参数:
            turn_id: 待清理快照的轮次标识。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``file_snapshots`` 表删除匹配 turn_id 的行。
        """
        with self._session_factory.begin() as session:
            session.execute(delete(FileSnapshotModel).where(FileSnapshotModel.turn_id == turn_id))
