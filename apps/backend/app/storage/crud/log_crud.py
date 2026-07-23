"""独立日志 SQLite 数据库的 CRUD 数据访问层。

单一职责：只提供日志库 ``log_entries`` 表的批量写入与条件查询，以及 record↔行数据的转换。
日志库是与主业务库分离的独立 SQLite 文件，避免高频日志写入与业务事务互相干扰。

职责边界：
- 负责：日志批量写入、按条件查询、``LogEntryRecord``↔行数据转换、查询参数校验。
- 不负责：日志格式化 / 采集 / 路由（见 ``config/logging``）、schema 初始化
  （由 ``init_storage`` → ``init_schema.initialize_log_schema`` 负责）、引擎生命周期。

依赖约定：构造时通过 ``log_session_factory()`` 取得日志库共享 session 工厂，必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

import json
from typing import Any

from sqlalchemy import Engine, Select, asc, desc, insert, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import LogEntryRecord, LogQuery
from app.storage.model.log_model import LogEntryModel
from app.storage.store_engines import log_engine, log_session_factory


class LogStore:
    """独立日志 SQLite 数据库的读写入口。

    仅负责日志条目的批量写入与条件查询，不承担日志采集 / 格式化与 schema 初始化；通过共享的日志库
    session 工厂访问数据库。
    """

    def __init__(self) -> None:
        """绑定日志库共享 session 工厂。

        引擎、连接池与 session 工厂由 ``app.storage.store_engines`` 统一创建与释放；日志库
        schema 由 ``init_storage`` 在进程启动时经 ``init_schema.initialize_log_schema`` 一次性
        初始化，本 store 仅复用，不重复初始化。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 ``init_storage`` 尚未调用（日志库 session 工厂不可用）。

        副作用:
            无（仅复用已由 ``init_storage`` 初始化好的日志库 session 工厂与 schema）。
        """
        self._session_factory = log_session_factory()

    def insert_many(self, entries: list[LogEntryRecord]) -> None:
        """批量写入日志记录。

        使用单条 ``INSERT`` 批量语句写入，空列表时直接返回、不开事务。``data`` 与 ``error``
        字段会被 JSON 序列化后存储。

        参数:
            entries: 待写入的日志记录列表；为空时不执行任何操作。

        返回:
            无。

        异常:
            TypeError: 如果某条记录的 data 或 error 无法 JSON 序列化。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向日志库 ``log_entries`` 表批量插入多行。
        """

        if not entries:
            return
        with self._session_factory.begin() as session:
            session.execute(insert(LogEntryModel), [_entry_values(entry) for entry in entries])

    def query(self, query: LogQuery) -> list[LogEntryRecord]:
        """按条件查询日志记录。

        先校验查询参数（limit、排序方向），再按 trace_id / level / event / 时间范围过滤，
        并按时间戳（辅以 id）排序、限制返回条数。

        参数:
            query: 查询参数（过滤条件、排序方向与 limit）。

        返回:
            匹配的日志记录列表，按 ``query.order`` 指定方向排序；无匹配时为空列表。

        异常:
            ValueError: 如果 limit 小于 1 或排序方向非 asc/desc。
            json.JSONDecodeError: 如果库中存储的 data_json / error_json 不是合法 JSON。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次日志库只读 session。
        """

        _validate_query(query)
        statement = _build_select(query)
        with self._session_factory() as session:
            rows = session.execute(statement).scalars().all()
        return [_entry_from_model(row) for row in rows]

    @property
    def engine(self) -> Engine:
        """返回当前 LogStore 使用的日志库 SQLAlchemy 引擎。

        引擎由 ``app.storage.store_engines`` 统一创建与缓存；本属性通过 ``log_engine()``
        访问器取得进程内唯一的日志库引擎，本 store 不创建、不持有、不释放引擎。

        参数:
            无。

        返回:
            日志库 SQLAlchemy 引擎。

        异常:
            RuntimeError: 如果 ``init_storage`` 尚未调用（日志库引擎不可用）。

        副作用:
            无。
        """

        return log_engine()

    @property
    def session_factory(self) -> sessionmaker[Session]:
        """返回当前 LogStore 使用的日志库 session 工厂。

        参数:
            无。

        返回:
            日志库 SQLAlchemy session 工厂。

        异常:
            无。

        副作用:
            无。
        """

        return self._session_factory


def _entry_values(entry: LogEntryRecord) -> dict[str, Any]:
    """把日志记录转换为与 ``log_entries`` 列名匹配的 INSERT 参数字典。

    ``data`` 恒序列化为 JSON 文本；``error`` 存在时序列化、否则写 None；``truncated`` 布尔值
    映射为 0/1。

    参数:
        entry: 待转换的日志记录。

    返回:
        与 ``log_entries`` 表列名一一对应的参数字典。

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
    """把 ``LogEntryModel`` 行转换为业务 ``LogEntryRecord``。

    ``data_json`` 反序列化后若不是 dict 则回退为空 dict；``error_json`` 为空时 error 记为
    None，非 dict 时同样回退为 None；可空文本列（trace_id/caller）为 None 时回退为空字符串。

    参数:
        row: 查询得到的 ``LogEntryModel`` 行。

    返回:
        对应的 ``LogEntryRecord``。

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
    """校验日志查询参数的合法性。

    参数:
        query: 待校验的查询参数。

    返回:
        无。

    异常:
        ValueError: 如果 limit 小于 1，或 order 不是 ``"asc"`` / ``"desc"``。

    副作用:
        无。
    """

    if query.limit < 1:
        raise ValueError("limit must be greater than zero")
    if query.order not in {"asc", "desc"}:
        raise ValueError("order must be asc or desc")


def _build_select(query: LogQuery) -> Select[tuple[LogEntryModel]]:
    """根据查询参数构造可执行的 SQLAlchemy SELECT。

    仅对非空过滤条件追加 WHERE：trace_id / level / event 精确匹配，start_time / end_time
    限定时间范围；排序以 ts 为主、id 为辅（同方向），并应用 limit。

    参数:
        query: 已通过校验的查询参数。

    返回:
        构造好的 SELECT 语句（未执行）。

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
