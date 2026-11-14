"""Session context 消息行的行级事务内 CRUD。"""

from __future__ import annotations

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.session_context import SessionContextRecord
from app.storage.model.session_context_model import (
    AWAITING_STATUS_CONDITION,
    SessionContextModel,
)
from app.storage.store_engines import main_db_factory


class SessionContextCrud:
    """负责 context 消息行的存储读写。

    所有行↔对象映射统一经 :meth:`SessionContextRecord._from_model` /
    :meth:`SessionContextRecord._to_model`，不存在第二套序列化。
    """

    def __init__(self) -> None:
        """绑定共享主库 db 工厂。"""

        self._db_factory = main_db_factory()

    def get(
        self,
        session_id: int,
        include_in_context: bool = True,
        db: DbSession | None = None,
    ) -> list[SessionContextRecord]:
        """读取指定 session 的上下文消息行，按 ``sequence`` 升序返回。

        参数:
            session_id: 归属的 Session 标识。
            include_in_context: 为 ``True`` 时仅返回纳入上下文的行；为 ``False`` 时返回全部行。
            db: 外部事务 db；为 ``None`` 时自建只读事务。

        返回:
            记录列表；无匹配时返回空列表（不返回 ``None``）。
        """

        stmt = select(SessionContextModel).where(
            SessionContextModel.session_id == session_id
        )
        if include_in_context:
            stmt = stmt.where(SessionContextModel.include_in_context.is_(True))
        stmt = stmt.order_by(SessionContextModel.sequence.asc())
        if db is not None:
            rows = db.scalars(stmt).all()
        else:
            with self._db_factory.begin() as db:
                rows = db.scalars(stmt).all()
        return [SessionContextRecord._from_model(row) for row in rows]

    @staticmethod
    def _pending_background() -> ColumnElement[bool]:
        metadata = SessionContextModel.metadata_json
        return and_(
            func.json_type(metadata, "$.background") == "object",
            func.json_extract(metadata, "$.status") == "pending",
        )

    @staticmethod
    def awaiting_user() -> ColumnElement[bool]:
        """「等你操作」的 SQL 写法：``SessionContextRecord.awaits_user`` 的同一规则，改一处
        必须改另一处。

        tool 行只有等待占位与后台占位会以 ``pending`` 落库，没有后台工作记录的就是等待占位。
        状态那一项用字面量而不是绑定参数，并与部分索引 ``idx_session_context_awaiting`` 的条件逐字
        相同：SQLite 只有在查询条件能推出索引条件时才用部分索引，绑定参数推不出。
        """
        metadata = SessionContextModel.metadata_json
        return and_(
            text(AWAITING_STATUS_CONDITION),
            func.coalesce(func.json_type(metadata, "$.background"), "null") != "object",
            SessionContextModel.run_id.is_not(None),
            SessionContextModel.include_in_context.is_(True),
        )

    def list_unsettled_placeholders(
        self, session_id: int | None = None, db: DbSession | None = None
    ) -> list[SessionContextRecord]:
        """还在等结果的占位行：仍为 ``pending`` 的后台占位（metadata ``background`` 是工作记录），
        以及已接收决定、执行中（``running``）的 HITL 等待占位。

        tool 行只有这两类会以非终态落库（普通观察写入即终态），所以按状态即可识别。``session_id`` 为
        ``None`` 时跨全部 Session（重启收尾用）。按 metadata JSON 过滤，不改表结构。只看仍在上下文
        里的行：与决定入口、后台等待判定同一口径。
        """

        metadata_json = SessionContextModel.metadata_json
        status = func.json_extract(metadata_json, "$.status")
        stmt = select(SessionContextModel).where(
            SessionContextModel.run_id.is_not(None),
            # 被撤回的占位已移出上下文，不再结算：否则重启会把它推到「已中断」，并给这个
            # Session 多投一条重启通知。
            SessionContextModel.include_in_context.is_(True),
            or_(
                self._pending_background(),
                status == "running",
            ),
        )
        if session_id is not None:
            stmt = stmt.where(SessionContextModel.session_id == session_id)
        stmt = stmt.order_by(
            SessionContextModel.session_id.asc(), SessionContextModel.sequence.asc()
        )
        if db is not None:
            rows = db.scalars(stmt).all()
        else:
            with self._db_factory.begin() as db:
                rows = db.scalars(stmt).all()
        return [SessionContextRecord._from_model(row) for row in rows]

    def get_for_observation(
        self,
        session_id: int,
        turn: int | None = None,
    ) -> list[SessionContextRecord]:
        """读取观测页所需的全量消息，不按上下文或流式标记过滤；指定回合时只取这一回合的行。

        时间戳由完整持久化记录映射保留。
        """

        statement = select(SessionContextModel).where(
            SessionContextModel.session_id == session_id
        )
        if turn is not None:
            statement = statement.where(SessionContextModel.turn == turn)
        statement = statement.order_by(SessionContextModel.sequence.asc())
        with self._db_factory.begin() as db:
            rows = db.scalars(statement).all()
        return [SessionContextRecord._from_model(row) for row in rows]

    def get_by_session_sequence(
        self,
        session_id: int,
        sequence: int,
        db: DbSession | None = None,
    ) -> SessionContextRecord | None:
        """按 Session 与全局序号读取唯一 context 行。

        参数:
            session_id: context 所属 Session。
            sequence: Session 范围内唯一的消息序号。
            db: 外部事务 db；为 None 时自建只读事务。

        返回:
            匹配记录；不存在时返回 None。

        异常:
            sqlalchemy.exc.MultipleResultsFound: 若数据库唯一约束损坏导致出现重复行。

        副作用:
            无；只读取并反序列化一条记录。
        """

        statement = select(SessionContextModel).where(
            SessionContextModel.session_id == session_id,
            SessionContextModel.sequence == sequence,
        )
        if db is not None:
            row = db.scalars(statement).one_or_none()
        else:
            with self._db_factory.begin() as owned_db:
                row = owned_db.scalars(statement).one_or_none()
        return SessionContextRecord._from_model(row) if row is not None else None

    def max_sequence(self, session_id: int, db: DbSession | None = None) -> int:
        """返回指定 session 下 ``sequence`` 列的最大值；无行时返回 0。

        参数:
            session_id: 归属的 Session 标识。

        返回:
            该 session 下 ``sequence`` 最大值（无行时为 0），可直接作为下一个序号种子。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 查询失败时抛出。
        """

        statement = select(func.coalesce(func.max(SessionContextModel.sequence), 0)).where(
            SessionContextModel.session_id == session_id
        )
        if db is not None:
            current = db.scalar(statement)
        else:
            with self._db_factory.begin() as owned_db:
                current = owned_db.scalar(statement)
        return int(current or 0)

    def max_turn(self, session_id: int, db: DbSession | None = None) -> int:
        """返回指定 session 下最大的回合号（含已被编辑移出的行，回合号不复用）；无行时返回 0。"""

        statement = select(func.coalesce(func.max(SessionContextModel.turn), 0)).where(
            SessionContextModel.session_id == session_id
        )
        if db is not None:
            current = db.scalar(statement)
        else:
            with self._db_factory.begin() as owned_db:
                current = owned_db.scalar(statement)
        return int(current or 0)

    def create(
        self,
        record: SessionContextRecord,
        db: DbSession | None = None,
    ) -> None:
        """写入一条上下文消息行。

        参数:
            record: 待持久化的记录（含消息事实、模型上下文标记、Transport 投影标记与 sequence）。
            db: 外部事务 db；为 ``None`` 时自建事务并提交。

        返回:
            无。

        异常:
            sqlalchemy.exc.IntegrityError: 调用方分配的序号撞上 ``(session_id, sequence)`` 唯一约束，
                或其它数据库错误——一律向上抛出，不在此吞掉。

        副作用:
            传入 ``db`` 时仅 ``add`` 不提交（由调用方事务收口）；
            传入 ``None`` 时自建事务并在退出时提交。
        """

        model = record._to_model()
        if db is None:
            with self._db_factory.begin() as owned_db:
                self.create(record, db=owned_db)
            return
        # 外部事务是唯一提交边界；SQLite legacy 模式下首个 SAVEPOINT 的释放会提前提交。
        db.add(model)
        db.flush()

    def replace_message(
        self,
        session_id: int,
        sequence: int,
        record: SessionContextRecord,
        db: DbSession | None = None,
    ) -> None:
        """原子替换同一条 context 行的内容：消息正文、归属、模型与展示标记及完整角色 metadata。

        本方法是「原地替换一行」的唯一入口（占位被真实工具结果覆盖等操作都
        经它），因此**不改变行身份与顺序**：``id`` / ``session_id`` / ``sequence`` 保持原值，调用方
        必须保证 ``record`` 与目标 session/sequence 匹配。

        metadata 与正文整体替换，不保留旧等待请求或错误；旁路状态更新使用下方的原子 JSON 更新。

        参数:
            session_id: 目标行所属 Session。
            sequence: 目标行的序号（行身份）。
            record: 覆盖用的新内容；其 ``session_id`` / ``sequence`` 必须与入参一致。
            db: 外部事务 db；为 ``None`` 时自建事务并提交。

        异常:
            ValueError: ``record`` 的 session/sequence 与目标不匹配。
            KeyError: 目标行不存在。

        副作用:
            就地更新该行的 ``run_id`` / ``message_json`` / ``include_in_context`` /
            ``project_to_transport`` 与完整 ``metadata_json``；不新增、不删除行。
        """

        if record.session_id != session_id or record.sequence != sequence:
            raise ValueError("context replacement target does not match record")
        statement = select(SessionContextModel).where(
            SessionContextModel.session_id == session_id,
            SessionContextModel.sequence == sequence,
        )
        if db is None:
            with self._db_factory.begin() as owned_db:
                self.replace_message(session_id, sequence, record, db=owned_db)
            return
        model = db.scalar(statement)
        if model is None:
            raise KeyError(f"context row {session_id}/{sequence} not found")
        replacement = record._to_model()
        model.run_id = replacement.run_id
        model.message_json = replacement.message_json
        model.metadata_json = replacement.metadata_json
        model.include_in_context = replacement.include_in_context
        model.project_to_transport = replacement.project_to_transport
        db.flush()

    def exclude_rows(self, session_id: int, row_ids: frozenset[int], db: DbSession) -> None:
        """把这些行移出模型上下文与前端投影；行本身保留，供观测页查看。

        移出哪些行由撤回用例按回合与调用归属决定（见 ``turn_withdrawal_service``）。行不删除，所以
        新消息必须追加在 :meth:`max_sequence` 之后，不得复用这里任何行的序号。
        """
        if not row_ids:
            return
        db.execute(
            update(SessionContextModel)
            .where(
                SessionContextModel.session_id == session_id,
                SessionContextModel.id.in_(row_ids),
            )
            .values(include_in_context=False, project_to_transport=False)
        )

    def delete_by_session_ids(self, session_ids: list[int], db: DbSession | None = None) -> None:
        """按一批 session 的标识批量删除其全部上下文消息行。

        参数:
            session_ids: 待清理的 session 整数 id 列表。
            db: 可选外部事务 db；传入时复用该事务不自行提交，为 None 时
                自开事务并自动提交。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``session_contexts`` 表删除 ``session_id`` 命中的行；session_ids 为空
            或对应行不存在时静默无操作。
        """

        if not session_ids:
            return
        if db is not None:
            db.execute(
                delete(SessionContextModel).where(
                    SessionContextModel.session_id.in_(session_ids)
                )
            )
            return
        with self._db_factory.begin() as db:
            db.execute(
                delete(SessionContextModel).where(
                    SessionContextModel.session_id.in_(session_ids)
                )
            )
