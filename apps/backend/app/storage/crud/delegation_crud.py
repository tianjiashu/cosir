"""``delegations`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``delegations`` 单表的增删改查与 model↔record 转换。

职责边界：
- 负责：delegation 单表读写、并发额度原子检查与创建、``DelegationModel``↔
  ``DelegationRecord`` 转换。
- 不负责：跨表级联（由上层 service 编排）、业务规则校验。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，
必须在 ``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

import json

from sqlalchemy import asc, delete, func, insert, select, update

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
            原样返回已写入的 delegation 记录。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果插入 delegation 记录失败。

        副作用:
            向 ``delegations`` 表插入一行。
        """

        with self._session_factory.begin() as session:
            session.add(DelegationModel(**_to_model(record)))
        return record

    def create_pending_if_slot_available(
        self,
        record: DelegationRecord,
        max_concurrency: int,
    ) -> str | None:
        """在同一事务内按活跃额度原子创建 pending delegation。

        参数:
            record: 待持久化的 pending delegation 领域值对象。
            max_concurrency: 同一 parent turn 下允许同时活跃（``pending`` /
                ``running``）的 child delegation 数量上限。

        返回:
            额度未满时返回新建 delegation 的标识；额度已满时返回 ``None`` 且不写入记录。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果事务执行失败。

        副作用:
            通过原生连接执行 ``BEGIN IMMEDIATE`` 取得 SQLite 立即写锁（ORM session 的
            autobegin 会开 DEFERRED 事务而静默忽略 IMMEDIATE 指令，故此处绕过 ORM 事务
            管理直接控制连接），使同一 parent turn 的并发 acquire 在「统计活跃数 + 插入」
            边界排队；同一事务内先统计该 parent turn 下 active 记录数，额度已满则 ``rollback``
            返回 ``None``，未满则 ``insert`` 后 ``commit``。本方法是并发安全 acquire 的
            唯一事务路径；``list_active_by_parent_turn()`` 仅可用于查询展示，不能
            作为并发安全依据。额度已满被拒时写一条 ``info`` 级日志（含 ``parent_turn_id``、
            ``active_count``、``max_concurrency``）以便观测并发拒绝频次，不视为错误路径。
        """

        engine = self._session_factory.kw["bind"]
        with engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            active_count = conn.scalar(
                select(func.count(DelegationModel.delegation_id)).where(
                    DelegationModel.parent_turn_id == record.parent_turn_id,
                    DelegationModel.status.in_(ACTIVE_DELEGATION_STATUSES),
                )
            )
            if (active_count or 0) >= max_concurrency:
                from app.config.logging.logger import log

                log.info(
                    "delegation slot unavailable: parent_turn_id=%s active_count=%s "
                    "max_concurrency=%s, acquire rejected",
                    record.parent_turn_id,
                    active_count or 0,
                    max_concurrency,
                )
                conn.rollback()
                return None
            conn.execute(insert(DelegationModel).values(**_to_model(record)))
            conn.commit()
        return record.delegation_id

    def update_status(
        self,
        delegation_id: str,
        status: str,
        child_turn_id: str | None = None,
        child_task_id: str | None = None,
        summary: str | None = None,
        error: str | None = None,
    ) -> DelegationRecord:
        """更新 delegation 状态及可选的 child 执行字段。

        参数:
            delegation_id: 待更新 delegation 的标识。
            status: 新的 delegation 状态。
            child_turn_id: 可选的 child turn 标识；传入时覆盖原值。
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

        self.get(delegation_id)
        values: dict[str, str] = {"status": status, "updated_at": to_text(utc_now())}
        if child_turn_id is not None:
            values["child_turn_id"] = child_turn_id
        if child_task_id is not None:
            values["child_task_id"] = child_task_id
        if summary is not None:
            values["summary"] = summary
        if error is not None:
            values["error"] = error
        with self._session_factory.begin() as session:
            session.execute(
                update(DelegationModel)
                .where(DelegationModel.delegation_id == delegation_id)
                .values(**values)
            )
        return self.get(delegation_id)

    def get(self, delegation_id: str) -> DelegationRecord:
        """按标识返回单条 delegation 记录。

        参数:
            delegation_id: delegation 标识。

        返回:
            匹配的 delegation 记录。

        异常:
            KeyError: 如果 delegation 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果读取 delegation 记录失败。

        副作用:
            打开一次主数据库只读 session。
        """

        with self._session_factory() as session:
            row = session.get(DelegationModel, delegation_id)
        if row is None:
            raise KeyError(delegation_id)
        return DelegationRecord.from_model(row)

    def list_by_parent_turn(self, parent_turn_id: str) -> list[DelegationRecord]:
        """列出某 parent turn 下的 delegation，按创建时间升序。

        参数:
            parent_turn_id: parent turn 标识。

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
                    .where(DelegationModel.parent_turn_id == parent_turn_id)
                    .order_by(asc(DelegationModel.created_at), asc(DelegationModel.delegation_id))
                )
                .scalars()
                .all()
            )
        return [DelegationRecord.from_model(row) for row in rows]

    def delete_by_ids(self, ids: list[str]) -> int:
        """按任务标识批量删除 delegation 记录。

        参数:
            ids: 待清理 delegation 的任务标识列表。

        返回:
            被删除的 delegation 行数（便于调用方审计日志）。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``delegations`` 表删除 ``task_id`` 命中的行；仅删除 delegation 自身，
            不级联其他表（级联编排由上层 service 负责）。ids 为空或对应行不存在时
            静默无操作；返回 0。
        """

        if not ids:
            return 0
        with self._session_factory.begin() as session:
            result = session.execute(
                delete(DelegationModel).where(DelegationModel.task_id.in_(ids))
            )
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
                    .order_by(asc(DelegationModel.created_at), asc(DelegationModel.delegation_id))
                )
                .scalars()
                .all()
            )
        return [DelegationRecord.from_model(row) for row in rows]


def _serialize_tools(tools: tuple[str, ...]) -> str:
    """将工具标识元组序列化为 JSON 数组文本（供 JSON 列存储）。

    参数:
        tools: 要持久化的工具标识元组。

    返回:
        JSON 数组文本。

    异常:
        TypeError: 如果工具集合包含无法 JSON 序列化的值。

    副作用:
        无。
    """

    return json.dumps(tools)


def _to_model(record: DelegationRecord) -> dict[str, str]:
    """从领域值对象提取 ORM 表的列值字典。

    统一 create 与 create_pending_if_slot_available 的字段映射。

    参数:
        record: 待持久化的委派记录值对象。

    返回:
        键为 ``DelegationModel`` 列名、值为已序列化列的字典。

    异常:
        无。

    副作用:
        无。
    """
    return {
        "delegation_id": record.delegation_id,
        "task_id": record.task_id,
        "parent_turn_id": record.parent_turn_id,
        "child_turn_id": record.child_turn_id,
        "child_task_id": record.child_task_id,
        "parent_agent_id": record.parent_agent_id,
        "child_agent_id": record.child_agent_id,
        "delegation_type": record.delegation_type,
        "status": record.status,
        "prompt": record.prompt,
        "summary": record.summary,
        "error": record.error,
        "effective_tools": _serialize_tools(record.effective_tools),
        "created_at": to_text(record.created_at),
        "updated_at": to_text(record.updated_at),
    }
