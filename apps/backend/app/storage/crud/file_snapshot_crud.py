"""``file_snapshots`` 表的纯 CRUD 数据访问层（Turn 回退文件快照）。

单一职责：只提供 ``file_snapshots`` 单表的读写，不承担运行时编排。
所有方法通过共享的主库 session 工厂访问数据库，必须在 ``init_storage`` 之后实例化。
"""

from sqlalchemy import delete, func, select

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
