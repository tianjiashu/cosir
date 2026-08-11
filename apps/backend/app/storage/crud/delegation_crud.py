"""Single-table CRUD operations for persisted delegations."""

import json

from sqlalchemy import asc, select, update

from app.models.delegation_record import DelegationRecord
from app.storage.model.delegation_model import DelegationModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import from_text, to_text, utc_now


class DelegationCrud:
    """Persist and retrieve delegation records from the main SQLite database."""

    def __init__(self) -> None:
        """Bind the initialized main database session factory.

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
        """Insert one delegation record.

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
            session.add(
                DelegationModel(
                    delegation_id=record.delegation_id,
                    task_id=record.task_id,
                    parent_turn_id=record.parent_turn_id,
                    child_turn_id=record.child_turn_id,
                    parent_agent_id=record.parent_agent_id,
                    child_agent_id=record.child_agent_id,
                    delegation_type=record.delegation_type,
                    status=record.status,
                    prompt=record.prompt,
                    summary=record.summary,
                    error=record.error,
                    effective_tools=_serialize_tools(record.effective_tools),
                    created_at=to_text(record.created_at),
                    updated_at=to_text(record.updated_at),
                )
            )
        return record

    def update_status(
        self,
        delegation_id: str,
        status: str,
        child_turn_id: str | None = None,
        summary: str | None = None,
        error: str | None = None,
    ) -> DelegationRecord:
        """Update a delegation status and optional child execution fields.

        参数:
            delegation_id: 待更新 delegation 的标识。
            status: 新的 delegation 状态。
            child_turn_id: 可选的 child turn 标识；传入时覆盖原值。
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
        """Get one delegation record by identifier.

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
        return _record_from_model(row)

    def list_by_parent_turn(self, parent_turn_id: str) -> list[DelegationRecord]:
        """List delegations belonging to a parent turn in creation order.

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
        return [_record_from_model(row) for row in rows]

    def list_pending_or_running(self) -> list[DelegationRecord]:
        """List active delegations in creation order.

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
                    .where(DelegationModel.status.in_(("pending", "running")))
                    .order_by(asc(DelegationModel.created_at), asc(DelegationModel.delegation_id))
                )
                .scalars()
                .all()
            )
        return [_record_from_model(row) for row in rows]


def _serialize_tools(tools: tuple[str, ...]) -> str:
    """Serialize a tool tuple for the JSON text storage columns.

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


def _deserialize_tools(value: str) -> tuple[str, ...]:
    """Deserialize a JSON text storage column into a tool tuple.

    参数:
        value: 数据库存储的 JSON 数组文本。

    返回:
        按存储顺序还原的工具标识元组。

    异常:
        json.JSONDecodeError: 如果 value 不是合法 JSON。
        TypeError: 如果 value 解码后不是 JSON 数组。

    副作用:
        无。
    """

    decoded = json.loads(value)
    if not isinstance(decoded, list):
        raise TypeError("delegation tool columns must contain a JSON array")
    return tuple(str(tool) for tool in decoded)


def _record_from_model(row: DelegationModel) -> DelegationRecord:
    """Convert one ORM row into its immutable domain record.

    参数:
        row: 从 ``delegations`` 表读取的 ORM 行。

    返回:
        对应的 delegation 领域值对象。

    异常:
        json.JSONDecodeError: 如果存储的工具 JSON 无法解析。

    副作用:
        无。
    """

    return DelegationRecord(
        delegation_id=row.delegation_id,
        task_id=row.task_id,
        parent_turn_id=row.parent_turn_id,
        child_turn_id=row.child_turn_id,
        parent_agent_id=row.parent_agent_id,
        child_agent_id=row.child_agent_id,
        delegation_type=row.delegation_type,
        status=row.status,
        prompt=row.prompt,
        summary=row.summary,
        error=row.error,
        effective_tools=_deserialize_tools(row.effective_tools),
        created_at=from_text(row.created_at),
        updated_at=from_text(row.updated_at),
    )
