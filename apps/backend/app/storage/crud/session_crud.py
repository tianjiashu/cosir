"""``sessions`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``sessions`` 单表的增删改查与 model↔record 转换。

职责边界：
- 负责：session 单表读写、``SessionModel``↔``SessionRecord`` 转换。
- 不负责：跨表级联（如删除 session 时清理 turn / run / trace，由 ``service/session/`` 编排）、
  业务规则校验、事务跨多表编排。

依赖约定：构造时通过 ``main_db_factory()`` 取得主库共享 db 工厂，因此必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

from typing import Any, cast

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session as DbSession

from app.models import SessionRecord
from app.models.session_extra import SessionExtra, SessionFork
from app.models.session_kind import FORK_SESSION_TYPES, USER_FACING_SESSION_TYPES, SessionType
from app.storage.model.session_model import SessionModel
from app.storage.store_engines import main_db_factory
from app.utils.datetime_utils import to_text, utc_now


class SessionCrud:
    """``sessions`` 表的纯 CRUD。

    仅负责单表读写，不承担跨表编排与业务规则；所有方法通过共享的主库 db 工厂访问数据库。
    """

    def __init__(self) -> None:
        """绑定主库共享 db 工厂。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 ``init_storage`` 尚未调用（主库 db 工厂不可用）。

        副作用:
            无（仅复用已初始化的主库 db 工厂，不创建 / 释放引擎）。
        """
        self._db_factory = main_db_factory()

    def ensure_session(self, db: DbSession, session_id: int) -> SessionRecord:
        session: SessionModel | None = db.get(SessionModel, session_id)
        if session is None:
            raise KeyError(session_id)
        return SessionRecord.from_model(session)

    def create(
            self,
            workspace_id: int,
            title: str,
            session_type: SessionType = SessionType.USER,
            parent_session_id: int | None = None,
            creation_command_id: str | None = None,
            db: DbSession | None = None,
            extra: SessionExtra | None = None,
            tool_schemas: list[dict[str, Any]] | None = None,
            derivation_depth: int = 0,
    ) -> SessionRecord:
        """新建一条 session 记录并落库。

        主键 ``id`` 由存储引擎自增分配，``created_at`` / ``updated_at`` 由本方法以当前 UTC 时间
        统一填充。子 Session（委派）只经 ``parent_session_id`` 挂到父 Session，不记录发起它的
        父 Run：子 Session 会被父 Agent 在后续多个 Run 中复用，创建时的 Run 无法代表归属。Agent
        身份不落在 Session 上，随入队输入在建 Run 时固化到 Run。

        参数:
            workspace_id: 所属工作区标识（整数 id）。
            title: 会话标题。
            session_type: SessionType 枚举，缺省为 ``SessionType.USER``。
            parent_session_id: 父会话标识（整数 id），子 Session 必填，用户会话为 None。
            creation_command_id: 前端创建命令标识，用于首条命令未被接受时回收 provisional Session。
            db: 可选的外部 SQLAlchemy 会话。传入时在本方法内复用该事务（调用方负责提交）；
                为 None 时由本方法自开事务并自动提交。
            extra: Session 扩展 JSON（如 fork 来源、派生调用）。
            tool_schemas: 创建时固化的模型工具 schema。
            derivation_depth: 派生深度；由派生方按 ``core.tool.derivation.child_depth`` 算出，用户新建
                与用户 fork 为 0。

        返回:
            落库成功的 ``SessionRecord``（含自增分配的 id 与填充的创建 / 更新时间）。

        异常:
            sqlalchemy.exc.IntegrityError: 如果违反外键约束或同一工作区内 creation_command_id 重复。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``sessions`` 表插入一行；当 ``db`` 为 None 时由本方法提交事务。
        """
        if db is None:
            with self._db_factory.begin() as db:
                return self._build_and_flush(
                    db,
                    workspace_id,
                    title,
                    session_type,
                    parent_session_id,
                    creation_command_id,
                    extra,
                    tool_schemas,
                    derivation_depth,
                )
        return self._build_and_flush(
            db,
            workspace_id,
            title,
            session_type,
            parent_session_id,
            creation_command_id,
            extra,
            tool_schemas,
            derivation_depth,
        )

    def _build_and_flush(
            self,
            db: DbSession,
            workspace_id: int,
            title: str,
            session_type: SessionType,
            parent_session_id: int | None,
            creation_command_id: str | None,
            extra: SessionExtra | None,
            tool_schemas: list[dict[str, Any]] | None = None,
            derivation_depth: int = 0,
    ) -> SessionRecord:
        """在给定会话中构造并 flush 一条 session 记录。

        参数:
            db: 目标 SQLAlchemy 会话（已开启的事务）。
            workspace_id: 所属工作区标识。
            title: 会话标题。
            session_type: 会话类型。
            parent_session_id: 委派父会话标识。
            creation_command_id: 触发建会话的命令标识。

        返回:
            已 flush 的 ``SessionRecord``。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 写入失败。

        副作用:
            在当前事务中插入一行会话；事务提交由调用方或 ``create`` 决定。
        """
        model = SessionModel(
            workspace_id=workspace_id,
            title=title,
            session_type=session_type,
            parent_session_id=parent_session_id,
            creation_command_id=creation_command_id,
            extra=(extra or SessionExtra()).model_dump(mode="json"),
            tool_schemas=tool_schemas or [],
            current_run_id=None,
            derivation_depth=derivation_depth,
        )
        db.add(model)
        db.flush()
        return SessionRecord.from_model(model)

    def next_fork_title(self, workspace_id: int, base_title: str, db: DbSession) -> str:
        """在外部事务中生成当前工作区内可读的 fork 标题。"""

        titles = set(
            db.scalars(
                select(SessionModel.title).where(SessionModel.workspace_id == workspace_id)
            ).all()
        )
        suffix = 1
        candidate = f"{base_title} {suffix}"
        while candidate in titles:
            suffix += 1
            candidate = f"{base_title} {suffix}"
        return candidate

    def list_fork_sources(self, workspace_id: int) -> dict[int, int]:
        """返回工作区内 fork 与其直接来源的对应（fork id → 来源 id）；没有来源的 fork 不在其中。"""

        with self._db_factory() as db:
            rows = db.execute(
                select(SessionModel.id, SessionModel.extra).where(
                    SessionModel.workspace_id == workspace_id, SessionModel.session_type.in_(FORK_SESSION_TYPES)
                )
            ).all()
        sources: dict[int, int] = {}
        for row_id, raw_extra in rows:
            fork = cast(SessionFork, SessionExtra.model_validate(raw_extra).fork)
            if fork.tree_parent_session_id is not None:
                sources[row_id] = fork.tree_parent_session_id
        return sources

    def reparent_forks(
        self,
        workspace_id: int,
        source_ids: frozenset[int],
        new_source_id: int | None,
        db: DbSession,
    ) -> int:
        """在外部事务中把来源在 ``source_ids`` 里的 fork 改挂到 ``new_source_id``，返回改动行数。

        fork 的来源记在 ``extra.fork.tree_parent_session_id``（不是 ``parent_session_id``，后者只承载所有权）。
        ``new_source_id`` 为 ``None`` 时去掉来源，侧栏把它当作根。``source_ids`` 自身不改：它们随后
        被删除。刻意不刷新 ``updated_at``：侧栏按它排序，重挂不是对话活动。
        """

        rows = db.execute(
            select(SessionModel.id, SessionModel.extra).where(
                SessionModel.workspace_id == workspace_id,
                SessionModel.session_type.in_(FORK_SESSION_TYPES),
                SessionModel.id.not_in(source_ids),
            )
        ).all()
        moved = 0
        for row_id, raw_extra in rows:
            extra = SessionExtra.model_validate(raw_extra)
            fork = cast(SessionFork, extra.fork)
            if fork.tree_parent_session_id not in source_ids:
                continue
            updated = extra.model_copy(update={
                "fork": fork.model_copy(update={"tree_parent_session_id": new_source_id})
            })
            db.execute(
                update(SessionModel)
                .where(SessionModel.id == row_id)
                .values(extra=updated.model_dump(mode="json"),
                        updated_at=SessionModel.updated_at)
            )
            moved += 1
        return moved

    def list_by_parent_session(self, parent_session_id: int) -> list[SessionRecord]:
        """返回某父会话直接委派的子 Agent Session（``session_type='delegated'``），供展示使用。

        只看一层；需要整棵会话树（如删除）时用 ``list_child_ids``。
        """
        stmt = (
            select(SessionModel)
            .where(
                SessionModel.parent_session_id == parent_session_id,
                SessionModel.session_type == SessionType.DELEGATED,
            )
            .order_by(SessionModel.created_at.asc(), SessionModel.id.asc())
        )
        with self._db_factory() as db:
            rows = db.scalars(stmt).all()
        return [SessionRecord.from_model(row) for row in rows]

    def list_by_workspace_and_type(self, workspace_id: int, session_type: SessionType) -> list[SessionRecord]:
        """工作区内某一类型的全部 Session，按创建先后。"""
        stmt = (
            select(SessionModel)
            .where(SessionModel.workspace_id == workspace_id, SessionModel.session_type == session_type)
            .order_by(SessionModel.created_at.asc(), SessionModel.id.asc())
        )
        with self._db_factory() as db:
            return [SessionRecord.from_model(row) for row in db.scalars(stmt).all()]

    def list_child_ids(self, parent_session_id: int, db: DbSession) -> list[int]:
        """在调用方事务内返回挂在某父会话下的全部直接子 Session id，不区分 ``session_type``。

        会话树遍历的唯一入口：``sessions.parent_session_id`` 自引用外键不区分子 Session 类型，按类型过滤
        会让删除漏掉子行而触发外键失败。
        """
        return list(
            db.scalars(select(SessionModel.id).where(SessionModel.parent_session_id == parent_session_id))
        )

    def list_by_parent_session_ids(self, parent_session_ids: list[int]) -> dict[int, list[SessionRecord]]:
        """批量读取一组会话直接委派的子 Agent Session。"""

        if not parent_session_ids:
            return {}
        with self._db_factory() as db:
            rows = db.scalars(
                select(SessionModel)
                .where(
                    SessionModel.parent_session_id.in_(parent_session_ids),
                    SessionModel.session_type == SessionType.DELEGATED,
                )
                .order_by(SessionModel.created_at.asc(), SessionModel.id.asc())
            ).all()
        grouped = {session_id: [] for session_id in parent_session_ids}
        for row in rows:
            grouped[row.parent_session_id].append(SessionRecord.from_model(row))
        return grouped

    def get(self, session_id: int) -> SessionRecord:
        """按标识返回单个 session。

        参数:
            session_id: 会话标识（整数 id）。

        返回:
            匹配的 ``SessionRecord``。

        异常:
            KeyError: 如果指定 session 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 db。
        """
        with self._db_factory() as db:
            row: SessionModel | None = db.get(SessionModel, session_id)
        if row is None:
            raise KeyError(session_id)
        return SessionRecord.from_model(row)

    def set_current_run_id(
            self,
            session_id: int,
            run_id: int,
            db: DbSession | None = None,
    ) -> SessionRecord:
        """在同一事务中更新 Session 当前 Run 引用。"""

        values: dict[str, object] = {
            "current_run_id": run_id,
            "updated_at": to_text(utc_now()),
        }
        statement = (
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(**values)
        )
        if db is not None:
            result = db.execute(statement)
            if not result.rowcount:
                raise KeyError(session_id)
            db.flush()
            row: SessionModel | None = db.get(SessionModel, session_id)
            assert row is not None
            return SessionRecord.from_model(row)
        with self._db_factory.begin() as owned_db:
            result = owned_db.execute(statement)
            if not result.rowcount:
                raise KeyError(session_id)
        return self.get(session_id)

    def update_title(self, session_id: int, title: str) -> bool:
        """改写 Session 标题，返回是否真的变了；刻意不刷新 ``updated_at``：侧栏按它排序，改名不该让对话换位置。

        标题与原值相同时不写库（标题支线常生成与兜底标题相同的结果），调用方据返回值决定要不要通知。

        异常:
            KeyError: Session 不存在（含已被删除）。
        """

        with self._db_factory.begin() as db:
            current = db.scalar(select(SessionModel.title).where(SessionModel.id == session_id))
            if current is None:
                raise KeyError(session_id)
            if current == title:
                return False
            # 显式把 updated_at 写回原值：基类的 onupdate 对 Core update 同样生效。
            db.execute(
                update(SessionModel)
                .where(SessionModel.id == session_id)
                .values(title=title, updated_at=SessionModel.updated_at)
            )
            return True

    def list_by_workspace(self, workspace_id: int) -> list[SessionRecord]:
        """侧栏会话（``USER_FACING_SESSION_TYPES``），按更新时间倒序；委派子会话不在侧栏。"""
        stmt = (
            select(SessionModel)
            .where(
                SessionModel.workspace_id == workspace_id,
                SessionModel.session_type.in_(USER_FACING_SESSION_TYPES),
            )
            .order_by(SessionModel.updated_at.desc(), SessionModel.id.desc())
        )
        with self._db_factory() as db:
            return [SessionRecord.from_model(row) for row in db.scalars(stmt).all()]

    def list_existing_ids(self, session_ids: set[int]) -> set[int]:
        """``session_ids`` 中仍存在的那些（只查 ``id`` 列，打开一次只读 db）。"""
        stmt = select(SessionModel.id).where(SessionModel.id.in_(session_ids))
        with self._db_factory() as db:
            return set(db.scalars(stmt).all())

    def list_ids_by_workspace(self, workspace_id: int, db: DbSession | None = None) -> list[int]:
        """仅返回某工作区下全部 session 的整数 id 列表。

        本方法只查 ``id`` 一列，用于跨表级联删除等只需 id
        的场景，避免整行读取。

        参数:
            workspace_id: 工作区标识（整数 id）。
            db: 可选的调用方事务 db；传入时复用当前事务，不自行提交。

        返回:
            该工作区的 session id 列表；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 db。
        """
        stmt = select(SessionModel.id).where(SessionModel.workspace_id == workspace_id)
        if db is not None:
            return list(db.scalars(stmt).all())
        with self._db_factory() as owned_db:
            return list(owned_db.scalars(stmt).all())

    def list_ids_with_current_run_status(
            self, status: str, db: DbSession | None = None
    ) -> list[int]:
        """返回当前 Run 处于 ``status`` 的 session id；启动时找出挂起中、需要 actor 等唤醒的 Session。"""
        from app.storage.model.run_model import RunModel

        stmt = (
            select(SessionModel.id)
            .join(RunModel, RunModel.id == SessionModel.current_run_id)
            .where(RunModel.status == status)
        )
        if db is not None:
            return list(db.scalars(stmt).all())
        with self._db_factory() as owned_db:
            return list(owned_db.scalars(stmt).all())

    def list_root_ids_by_workspace(
            self, workspace_id: int, db: DbSession | None = None
    ) -> list[int]:
        """返回某工作区下「主会话」（``parent_session_id`` 为 NULL）的整数 id 列表。

        主会话对应于用户会话 / fork 会话等根节点，其下通过 ``parent_session_id`` 自引用挂接
        委派子会话。级联删除工作区时只需删除这些根会话，``SessionService.delete_session`` 会沿
        ``parent_session_id`` 递归清理整棵子树（含委派子会话），从而覆盖工作区内全部 session。

        参数:
            workspace_id: 工作区标识（整数 id）。
            db: 可选的调用方事务 db；传入时复用当前事务，不自行提交。

        返回:
            该工作区下根会话（``parent_session_id IS NULL``）的 id 列表；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 db。
        """

        stmt = select(SessionModel.id).where(
            SessionModel.workspace_id == workspace_id,
            SessionModel.parent_session_id.is_(None),
        )
        if db is not None:
            return list(db.scalars(stmt).all())
        with self._db_factory() as owned_db:
            return list(owned_db.scalars(stmt).all())

    def delete_by_ids(self, session_ids: list[int], db: DbSession | None = None) -> None:
        """按标识批量删除 session。

        参数:
            session_ids: 待删除的 session 整数 id 列表。
            db: 可选外部事务 db；传入时复用该事务不自行提交，为 None 时
                自开事务并自动提交。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``sessions`` 表删除匹配的行。仅删除 session 自身，不级联清理 turn / run / trace
            （级联由上层 service 编排）；session_ids 为空或对应行不存在时静默无操作。
        """

        if not session_ids:
            return
        if db is not None:
            db.execute(delete(SessionModel).where(SessionModel.id.in_(session_ids)))
            return
        with self._db_factory.begin() as db:
            db.execute(delete(SessionModel).where(SessionModel.id.in_(session_ids)))
