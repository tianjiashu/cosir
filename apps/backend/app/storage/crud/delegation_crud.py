"""``delegations`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``delegations`` 单表的增删改查与 model↔record 转换。

职责边界：
- 负责：delegation 单表读写、并发额度原子检查与创建、``DelegationModel``↔
  ``DelegationRecord`` 转换。
- 不负责：跨表级联（由上层 service 编排）、业务规则校验。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，
必须在 ``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

import dataclasses

from sqlalchemy import ColumnElement, asc, delete, func, insert, or_, select, update
from sqlalchemy.orm import Session

from app.models.delegation_record import DelegationRecord
from app.storage.model.delegation_model import DelegationModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now

ACTIVE_DELEGATION_STATUSES = ("pending", "running")
"""视为「活跃」的 delegation 状态集合，用于并发额度统计与活跃列表查询。"""


class DelegationCrud:
    """``delegations`` 表的纯 CRUD。

    仅负责单表读写与 model↔record 转换，不承担跨表编排；所有方法通过共享主库
    session 工厂访问数据库。
    """

    def __init__(self) -> None:
        """绑定主库共享 session 工厂。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果主存储尚未初始化。

        副作用:
            保存进程级主数据库 session 工厂的引用。
        """

        self._session_factory = main_session_factory()

    def create(self, record: DelegationRecord) -> DelegationRecord:
        """插入一条 delegation 记录。

        参数:
            record: 待持久化的 delegation 领域值对象。

        返回:
            回填数据库自增 id 后的新建 ``DelegationRecord``。原 ``record`` 为不可变
            dataclass，故通过 ``dataclasses.replace`` 生成新实例携带 ``model.id``；
            调用方应改用返回值而非传入对象获取新建行主键。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果插入 delegation 记录失败。

        副作用:
            向 ``delegations`` 表插入一行。
        """

        with self._session_factory.begin() as session:
            model = DelegationModel(**record.to_model_dict())
            session.add(model)
        # record 是不可变 dataclass，自增 id 需通过 dataclasses.replace 回填后返回，
        # 否则调用方无法拿到新建行的主键。
        return dataclasses.replace(record, id=model.id)

    def create_pending_if_slot_available(
        self,
        record: DelegationRecord,
        max_concurrency: int,
    ) -> int | None:
        """在同一事务内按活跃额度原子创建 pending delegation。

        参数:
            record: 待持久化的 pending delegation 领域值对象。
            max_concurrency: 同一 parent turn 下允许同时活跃（``pending`` /
                ``running``）的 child delegation 数量上限。

        返回:
            额度未满时返回数据库为该行分配的自增主键标识（``int``，由
            ``Result.lastrowid`` 取回，而非传入 ``record.id``）；额度已满时返回
            ``None`` 且不写入记录。调用方应以 ``None`` 唯一判定额度已满，成功路径
            始终返回正整数标识。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果事务执行失败。

        副作用:
            通过原生连接执行 ``BEGIN IMMEDIATE`` 取得 SQLite 立即写锁（ORM session 的
            autobegin 会开 DEFERRED 事务而静默忽略 IMMEDIATE 指令，故此处绕过 ORM 事务
            管理直接控制连接），使同一 parent turn 的并发 acquire 在「统计活跃数 + 插入」
            边界排队；同一事务内先统计该 parent turn 下 active 记录数，额度已满则 ``rollback``
            返回 ``None``，未满则 ``insert`` 后 ``commit``。本方法是并发安全 acquire 的
            唯一事务路径；``list_active_by_parent_turn()`` 仅可用于查询展示，不能
            作为并发安全依据。额度已满被拒时写一条 ``info`` 级日志（含 ``parent_run_id``、
            ``active_count``、``max_concurrency``）以便观测并发拒绝频次，不视为错误路径。
        """

        engine = self._session_factory.kw["bind"]
        with engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            active_count = conn.scalar(
                select(func.count(DelegationModel.id)).where(
                    DelegationModel.parent_run_id == record.parent_run_id,
                    DelegationModel.status.in_(ACTIVE_DELEGATION_STATUSES),
                )
            )
            if (active_count or 0) >= max_concurrency:
                from app.config.logging.logger import log

                log.info(
                    "delegation slot unavailable: parent_run_id=%s active_count=%s "
                    "max_concurrency=%s, acquire rejected",
                    record.parent_run_id,
                    active_count or 0,
                    max_concurrency,
                )
                conn.rollback()
                return None
            result = conn.execute(insert(DelegationModel).values(**record.to_model_dict()))
            created_id: int | None = result.lastrowid
            conn.commit()
        return created_id

    def update_status(
        self,
        id: int,
        status: str,
        child_run_id: int | None = None,
        child_task_id: int | None = None,
        summary: str | None = None,
        error: str | None = None,
    ) -> DelegationRecord:
        """更新 delegation 状态及可选的 child 执行字段。

        参数:
            id: 待更新 delegation 的标识。
            status: 新的 delegation 状态。
            child_run_id: 可选的 child turn 标识；传入时覆盖原值。
            child_task_id: 可选的 child task 标识；传入时覆盖原值。
            summary: 可选的 child 执行摘要；传入时覆盖原值。
            error: 可选的失败信息；传入时覆盖原值。

        返回:
            更新后的 delegation 记录。

        异常:
            KeyError: 如果 delegation 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果更新 delegation 记录失败。

        副作用:
            更新 ``delegations`` 表的状态、传入的可选字段及更新时间。
        """

        self.get(id)
        values: dict[str, str | int] = {"status": status, "updated_at": to_text(utc_now())}
        if child_run_id is not None:
            values["child_run_id"] = child_run_id
        if child_task_id is not None:
            values["child_task_id"] = child_task_id
        if summary is not None:
            values["summary"] = summary
        if error is not None:
            values["error"] = error
        with self._session_factory.begin() as session:
            session.execute(
                update(DelegationModel).where(DelegationModel.id == id).values(**values)
            )
        return self.get(id)

    def fail_active_delegations(
        self,
        error: str,
        parent_run_id: int | None = None,
    ) -> list[int]:
        """原子地把活跃 delegation 直接置为 failed 并返回受影响 id。

        用于崩溃重启后的残留恢复：把 ``pending`` / ``running`` 状态的委派一次性、原子地
        标记为 ``failed``，避免「先 list 快照再逐条 update」的 TOCTOU（已在循环外推进为
        ``succeeded`` 的记录不会被误覆盖），以及中途崩溃导致残留活跃记录永久占用并发额度、
        使新委派被永久拒绝。

        ``UPDATE ... WHERE status IN (active) [AND parent_run_id=?] RETURNING id`` 在单条
        语句内完成条件判定与状态跃迁：已非活跃的记录不会进入更新集；语句要么全成功要么
        回滚，不存在部分残留。``parent_run_id`` 为 ``None`` 时跨全部 parent turn 恢复
        （进程级重启场景），传入时仅恢复单个 parent turn 的残留。

        参数:
            error: 写入这些委派的统一失败原因（通常为恢复场景说明）。
            parent_run_id: 可选的 parent turn 标识；传入时仅恢复该 turn 下残留活跃委派，
                为 ``None``（默认）时恢复全部 parent turn 的残留活跃委派。

        返回:
            被本次语句实际置为 ``failed`` 的 delegation 主键 ``id`` 列表；无活跃记录时
            返回空列表。调用方应仅基于返回列表补发失败事件，避免对未受影响记录误发事件。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            更新 ``delegations`` 表中匹配行的 ``status``、``error``、``updated_at``；
            不经过逐条 ``update_status``，不触发单条事件（事件由上层据返回 id 统一补发）。
        """

        conditions: list[ColumnElement[bool]] = [
            DelegationModel.status.in_(ACTIVE_DELEGATION_STATUSES)
        ]
        if parent_run_id is not None:
            conditions.append(DelegationModel.parent_run_id == parent_run_id)
        with self._session_factory.begin() as session:
            rows = session.execute(
                update(DelegationModel)
                .where(*conditions)
                .values(status="failed", error=error, updated_at=to_text(utc_now()))
                .returning(DelegationModel.id)
            ).all()
        return [row[0] for row in rows]

    def get(self, id: int) -> DelegationRecord:
        """按标识返回单条 delegation 记录。

        参数:
            id: delegation 主键（整数自增 id）。

        返回:
            匹配的 delegation 记录。

        异常:
            KeyError: 如果 delegation 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果读取 delegation 记录失败。

        副作用:
            打开一次主数据库只读 session。
        """

        with self._session_factory() as session:
            row: DelegationModel | None = session.get(DelegationModel, id)
        if row is None:
            raise KeyError(id)
        return DelegationRecord.from_model(row)

    def list_by_parent_turn(self, parent_run_id: int) -> list[DelegationRecord]:
        """列出某 parent turn 下的 delegation，按创建时间升序。

        参数:
            parent_run_id: parent turn 标识。

        返回:
            按创建时间和 delegation 标识升序排列的 delegation 记录。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询 delegation 记录失败。

        副作用:
            打开一次主数据库只读 session。
        """

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(DelegationModel)
                    .where(DelegationModel.parent_run_id == parent_run_id)
                    .order_by(asc(DelegationModel.created_at), asc(DelegationModel.id))
                )
                .scalars()
                .all()
            )
        return [DelegationRecord.from_model(row) for row in rows]

    def delete_by_task_ids(
        self, task_ids: list[int], session: Session | None = None
    ) -> int:
        """删除与一批任务相关的全部 delegation 记录。

        同时按 ``task_id`` 与 ``child_task_id`` 匹配：``task_id`` 命中本任务发起的委派
        （指向其子任务），``child_task_id`` 命中创建本任务的委派（本任务作为委派子任务）。
        单任务删除必须同时清理这两类，否则 ``delegations.child_task_id -> tasks.id`` 与
        ``tasks.delegation_id -> delegations.id`` 外键会在删除任务行时报
        ``FOREIGN KEY constraint failed``。

        参数:
            task_ids: 待清理的任务整数 id 列表（非 delegation 自身 id）。
            session: 可选外部事务 session；传入时复用该事务不自行提交，为 None 时
                自开事务并自动提交。

        返回:
            被删除的 delegation 行数（便于调用方审计日志）。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``delegations`` 表删除 ``task_id`` 或 ``child_task_id`` 命中的行；仅删除
            delegation 自身，不级联其他表（级联编排由上层 service 负责）。task_ids 为空或
            对应行不存在时静默无操作；返回 0。
        """

        if not task_ids:
            return 0
        stmt = delete(DelegationModel).where(
            or_(
                DelegationModel.task_id.in_(task_ids),
                DelegationModel.child_task_id.in_(task_ids),
            )
        )
        if session is not None:
            result = session.execute(stmt)
            return int(result.rowcount or 0)
        with self._session_factory.begin() as session:
            result = session.execute(stmt)
        return int(result.rowcount or 0)

    def list_pending_or_running(self) -> list[DelegationRecord]:
        """列出全部活跃 delegation，按创建时间升序。

        参数:
            无。

        返回:
            状态为 ``pending`` 或 ``running`` 的 delegation 记录。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询 delegation 记录失败。

        副作用:
            打开一次主数据库只读 session。
        """

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(DelegationModel)
                    .where(DelegationModel.status.in_(ACTIVE_DELEGATION_STATUSES))
                    .order_by(asc(DelegationModel.created_at), asc(DelegationModel.id))
                )
                .scalars()
                .all()
            )
        return [DelegationRecord.from_model(row) for row in rows]
