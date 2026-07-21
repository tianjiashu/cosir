"""日志数据库 CRUD。"""

import json
from typing import Any

from sqlalchemy import Engine, Select, asc, desc, insert, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import LogEntryRecord
from app.models import LogQuery
from app.storage.model.log_model import LogEntryModel
from app.storage.store_engines import log_engine, log_session_factory


class LogStore:
    """读写独立日志 SQLite 数据库。"""

    def __init__(self) -> None:
        """初始化日志数据库访问层。

        引擎、连接池与 session 工厂由 ``app.storage.engines`` 统一创建与释放；日志库
        schema 由 ``init_storage`` 在进程启动时一次性初始化，本 store 仅复用，不重复
        初始化。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 ``init_storage`` 尚未调用（日志库引擎不可用）。

        副作用:
            无（仅复用已由 ``init_storage`` 初始化好的日志库引擎与 schema）。
        """
        self._session_factory = log_session_factory()

    def insert_many(self, entries: list[LogEntryRecord]) -> None:
        """批量写入日志记录。

        参数:
            entries: 待写入的日志记录列表。

        返回:
            无。

        异常:
            TypeError: 如果 data 或 error 无法 JSON 序列化。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向日志 SQLite 写入多条记录。
        """

        if not entries:
            return
        with self._session_factory.begin() as session:
            session.execute(insert(LogEntryModel), [_entry_values(entry) for entry in entries])

    def query(self, query: LogQuery) -> list[LogEntryRecord]:
        """按条件查询日志记录。

        参数:
            query: 查询参数。

        返回:
            匹配的日志记录列表。

        异常:
            ValueError: 如果 limit 或排序方向非法。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            读取日志 SQLite。
        """

        _validate_query(query)
        statement = _build_select(query)
        with self._session_factory() as session:
            rows = session.execute(statement).scalars().all()
        return [_entry_from_model(row) for row in rows]

    @property
    def engine(self) -> Engine:
        """返回当前 LogStore 使用的 SQLAlchemy Engine。

        参数:
            无。

        返回:
            SQLAlchemy Engine。

        异常:
            无。

        副作用:
            无。
        """

        return self._engine

    @property
    def session_factory(self) -> sessionmaker[Session]:
        """返回当前 LogStore 使用的 Session 工厂。

        参数:
            无。

        返回:
            SQLAlchemy Session 工厂。

        异常:
            无。

        副作用:
            无。
        """

        return self._session_factory


def _entry_values(entry: LogEntryRecord) -> dict[str, Any]:
    """把日志记录转换为 INSERT 参数。

    参数:
        entry: 日志记录。

    返回:
        与 `log_entries` 表列名匹配的参数字典。

    异常:
        TypeError: 如果 data 或 error 无法 JSON 序列化。

    副作用:
        无。
    """

    return {
        "ts": entry.ts,
        "level": entry.level,
        "logger": entry.logger,
        "trace_id": entry.trace_id,
        "caller": entry.caller,
        "event": entry.event,
        "msg": entry.msg,
        "data_json": json.dumps(entry.data, ensure_ascii=False),
        "error_json": json.dumps(entry.error, ensure_ascii=False) if entry.error else None,
        "truncated": 1 if entry.truncated else 0,
    }


def _entry_from_model(row: LogEntryModel) -> LogEntryRecord:
    """把 SQLAlchemy model 转换为日志记录。

    参数:
        row: `LogEntryModel` 查询结果。

    返回:
        LogEntryRecord 实例。

    异常:
        json.JSONDecodeError: 如果 data_json 或 error_json 不是合法 JSON。

    副作用:
        无。
    """

    data = json.loads(row.data_json or "{}")
    error = json.loads(row.error_json) if row.error_json else None
    return LogEntryRecord(
        ts=row.ts,
        level=row.level,
        logger=row.logger,
        trace_id=row.trace_id or "",
        caller=row.caller or "",
        event=row.event,
        msg=row.msg,
        data=data if isinstance(data, dict) else {},
        error=error if isinstance(error, dict) else None,
        truncated=bool(row.truncated),
    )


def _validate_query(query: LogQuery) -> None:
    """校验查询参数。

    参数:
        query: 查询参数。

    返回:
        无。

    异常:
        ValueError: 如果 limit 或排序方向非法。

    副作用:
        无。
    """

    if query.limit < 1:
        raise ValueError("limit must be greater than zero")
    if query.order not in {"asc", "desc"}:
        raise ValueError("order must be asc or desc")


def _build_select(query: LogQuery) -> Select[tuple[LogEntryModel]]:
    """根据查询参数构造 SQLAlchemy SELECT。

    参数:
        query: 查询参数。

    返回:
        可执行的 SQLAlchemy SELECT。

    异常:
        无。

    副作用:
        无。
    """

    statement = select(LogEntryModel)
    for column, value in (
        (LogEntryModel.trace_id, query.trace_id),
        (LogEntryModel.level, query.level),
        (LogEntryModel.event, query.event_name),
    ):
        if value:
            statement = statement.where(column == value)
    if query.start_time:
        statement = statement.where(LogEntryModel.ts >= query.start_time)
    if query.end_time:
        statement = statement.where(LogEntryModel.ts <= query.end_time)
    order_column = asc(LogEntryModel.ts) if query.order == "asc" else desc(LogEntryModel.ts)
    id_order = asc(LogEntryModel.id) if query.order == "asc" else desc(LogEntryModel.id)
    return statement.order_by(order_column, id_order).limit(query.limit)
