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

from sqlalchemy import Select, asc, desc, func, insert, select

from app.models import LogEntryRecord, LogQuery
from app.storage.model.log_model import LogEntryModel
from app.storage.store_engines import log_session_factory


class LogCrud:
    """独立日志 SQLite 数据库的纯 CRUD。

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

    def query(self, query: LogQuery) -> tuple[list[LogEntryRecord], int]:
        """按条件分页查询日志记录。

        先校验查询参数（limit、offset、排序方向），再按 trace_id / level / event / 时间范围过滤，
        并按时间戳（辅以 id）排序、跳过 ``offset`` 条、限制返回 ``limit`` 条；同时统计匹配过滤条件
        的总记录数（不受 offset/limit 影响），用于前端分页。

        参数:
            query: 查询参数（过滤条件、排序方向、limit 与 offset）。

        返回:
            二元组 ``(entries, total)``：匹配的当页记录列表（按 ``query.order`` 指定方向排序，
            无匹配时为空列表）与满足过滤条件的总记录数。

        异常:
            ValueError: 如果 limit 小于 1、offset 小于 0 或排序方向非 asc/desc。
            json.JSONDecodeError: 如果库中存储的 data_json / error_json 不是合法 JSON。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次日志库只读 session。
        """

        _validate_query(query)
        select_stmt = _build_select(query)
        count_stmt = _build_count(query)
        with self._session_factory() as session:
            rows = session.execute(select_stmt).scalars().all()
            total = session.execute(count_stmt).scalar_one()
        return [LogEntryRecord.from_model(row) for row in rows], int(total)

    def count_by_level(self, query: LogQuery) -> dict[str, int]:
        """统计各日志级别的记录数（忽略 level 过滤、含其余过滤条件）。

        用于前端级别分布展示：即使调用方已按某级别筛选，仍返回该过滤集下各级别的完整计数，
        便于用户一眼看到分布并切换级别。

        参数:
            query: 查询参数（``level`` 字段被忽略；``trace_id`` / ``event_name`` / ``keyword`` /
                时间范围过滤条件仍生效）。

        返回:
            ``{level: count}`` 映射，仅包含有记录的级别；无匹配时返回空字典。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次日志库只读 session。
        """

        level_ignored = LogQuery(
            trace_id=query.trace_id,
            event_name=query.event_name,
            keyword=query.keyword,
            start_time=query.start_time,
            end_time=query.end_time,
        )
        statement = _apply_filters(
            select(LogEntryModel.level, func.count()).select_from(LogEntryModel),
            level_ignored,
        ).group_by(LogEntryModel.level)
        with self._session_factory() as session:
            rows = session.execute(statement).all()
        return {str(level): int(count) for level, count in rows}


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


def _validate_query(query: LogQuery) -> None:
    """校验日志查询参数的合法性。

    参数:
        query: 待校验的查询参数。

    返回:
        无。

    异常:
        ValueError: 如果 limit 小于 1、offset 小于 0，或 order 不是 ``"asc"`` / ``"desc"``。

    副作用:
        无。
    """

    if query.limit < 1:
        raise ValueError("limit must be greater than zero")
    if query.offset < 0:
        raise ValueError("offset must be greater than or equal to zero")
    if query.order not in {"asc", "desc"}:
        raise ValueError("order must be asc or desc")


def _escape_like(value: str) -> str:
    """转义 LIKE 模式中的通配符，使关键词按字面量匹配。

    SQLite ``LIKE`` 的 ``%`` / ``_`` / ``\\`` 是通配符；调用方传入的关键词若含这些字符会
    被误当成模式。本函数在每个通配符前插入转义符 ``\\``（即字面反斜杠），配合调用处的
    ``escape="\\"``（Python 字符串即单个反斜杠，告知 SQLite 转义符为 ``\\``），使模式中的
    ``\\%`` / ``\\_`` / ``\\\\`` 被解释为字面 ``%`` / ``_`` / ``\\``，从而实现精确子串匹配。

    转义顺序必须为 ``\\`` → ``%`` → ``_``：先转义反斜杠本身，避免后续插入的 ``\\`` 被二次转义。

    参数:
        value: 原始关键词。

    返回:
        转义后的关键词（``%`` / ``_`` / ``\\`` 前加 ``\\``）。

    异常:
        无。

    副作用:
        无。
    """

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _apply_filters(statement: Select[Any], query: LogQuery) -> Select[Any]:
    """向查询语句统一追加过滤条件（trace_id / level / event 精确匹配、keyword 子串、时间范围）。

    供 ``_build_select`` / ``_build_count`` / ``count_by_level`` 复用，确保分页、计数与级别
    分布统计的过滤条件完全一致，避免平行重复导致的条件漂移。

    参数:
        statement: 已构造 SELECT 主体的语句。
        query: 已通过校验的查询参数。

    返回:
        追加完 WHERE 条件的语句（未执行）。

    异常:
        无。

    副作用:
        无。
    """

    for column, value in (
        (LogEntryModel.trace_id, query.trace_id),
        (LogEntryModel.level, query.level),
        (LogEntryModel.event, query.event_name),
    ):
        if value:
            statement = statement.where(column == value)
    if query.keyword:
        statement = statement.where(
            LogEntryModel.msg.like(f"%{_escape_like(query.keyword)}%", escape="\\")
        )
    if query.start_time:
        statement = statement.where(LogEntryModel.ts >= query.start_time)
    if query.end_time:
        statement = statement.where(LogEntryModel.ts <= query.end_time)
    return statement


def _build_select(query: LogQuery) -> Select[tuple[LogEntryModel]]:
    """根据查询参数构造可执行的 SQLAlchemy SELECT。

    仅对非空过滤条件追加 WHERE（经 ``_apply_filters``），排序以 ts 为主、id 为辅（同方向），
    并应用 offset 与 limit 实现分页。

    参数:
        query: 已通过校验的查询参数。

    返回:
        构造好的 SELECT 语句（未执行，含 order_by / offset / limit）。

    异常:
        无。

    副作用:
        无。
    """

    statement = _apply_filters(select(LogEntryModel), query)
    order_column = asc(LogEntryModel.ts) if query.order == "asc" else desc(LogEntryModel.ts)
    id_order = asc(LogEntryModel.id) if query.order == "asc" else desc(LogEntryModel.id)
    return statement.order_by(order_column, id_order).offset(query.offset).limit(query.limit)


def _build_count(query: LogQuery) -> Select[tuple[int]]:
    """根据查询参数构造匹配记录总数的 COUNT 语句。

    复用与 ``_build_select`` 相同的过滤条件（经 ``_apply_filters``，不含 order_by / offset /
    limit），用于分页时向前端返回满足过滤条件的总记录数。

    参数:
        query: 已通过校验的查询参数。

    返回:
        构造好的 ``SELECT COUNT(*)`` 语句（未执行）。

    异常:
        无。

    副作用:
        无。
    """

    return _apply_filters(select(func.count()).select_from(LogEntryModel), query)
