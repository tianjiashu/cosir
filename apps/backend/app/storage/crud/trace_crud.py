"""``trace_events`` 与 ``trace_spans`` 表的纯 CRUD 数据访问层（运行追踪）。

单一职责：只提供 trace 追踪两张表的读写与 record↔model 转换，以及 trace 摘要聚合查询。
trace event 记录运行过程中的离散事件（带按 run 递增的 sequence_no），trace span 记录带时长
的区间；二者共同构成一次运行的可观测追踪骨架。

关于序列号并发保护：
    ``sequence_no`` 是“按 run_id 分组的应用层局部自增序号”，SQLite 的 AUTOINCREMENT 无法
    按 run 重置，只能用 ``SELECT MAX(sequence_no)+1 WHERE run_id=?`` 自算，这构成读-改-写
    竞争。因此 ``append_event`` 用进程内共享锁把“算序号 + 插入”包成原子操作，避免同一 run
    内序号重复。锁按 session_factory 实例定位，同一物理库的多个 store 实例复用同一把锁。

职责边界：
- 负责：trace event / span 单表读写、摘要聚合、record↔model 转换、序列号并发保护。
- 不负责：trace 语义编排与脱敏（见 ``service/trace``、``trace_infra``）、引擎生命周期。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

import json
from dataclasses import replace
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from sqlalchemy import asc, delete, desc, func, select
from sqlalchemy.orm import sessionmaker

from app.models import TraceEventRecord
from app.models import TraceSpanRecord
from app.storage.model.trace_model import TraceEventModel, TraceSpanModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import from_text as _from_text


class TraceStore:
    """``trace_events`` 与 ``trace_spans`` 表的纯 CRUD。

    仅负责 trace 两表读写、摘要聚合与序列号并发保护，不承担 trace 语义编排；通过共享主库 session
    工厂访问数据库。类级共享的 ``_sequence_locks`` 按 session_factory 定位序列锁，保证同一物理库
    的序列号分配跨实例互斥。
    """

    _sequence_locks: dict[sessionmaker, Lock] = {}
    _sequence_locks_guard = Lock()

    def __init__(self) -> None:
        """初始化 trace store 并绑定序列锁。

        主库 session 工厂由 ``app.storage.store_engines`` 统一创建与释放；本 store 直接复用，
        不持有、不 dispose 共享 engine。session_factory 同时作为进程内共享序列锁的定位键：
        同一物理数据库共享的 session_factory 会复用同一把锁。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 ``init_storage`` 尚未调用（主库 session 工厂不可用）。

        副作用:
            为该 session_factory 登记 / 复用进程内共享序列锁。
        """

        self._session_factory = main_session_factory()
        self._sequence_lock = self._lock_for_session_factory(self._session_factory)

    def close(self) -> None:
        """释放本 store 持有的进程内资源（当前为空操作，仅保留兼容）。

        TraceStore 共享 ``app.storage.store_engines`` 的主库 engine 与连接池，不自行 dispose
        共享引擎（否则会关闭其他 CRUD 共用的连接池）；共享 engine 的生命周期由
        ``close_storage()`` 统一负责。本方法保留仅为兼容既有调用。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            无（不释放共享资源）。
        """

        return

    def next_sequence(self, run_id: str) -> int:
        """预览某 run 内的下一个 trace event 序号（只读，不占用序列锁）。

        用 ``SELECT MAX(sequence_no)+1`` 计算，仅供预览 / 展示。注意：本方法未加序列锁，不保证
        并发下与后续 ``append_event`` 实际分配的序号一致；真正的序号分配以 ``append_event`` 为准。

        参数:
            run_id: 运行标识。

        返回:
            该 run 当前的下一个序号（从 1 起）。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            value = session.execute(
                select(func.coalesce(func.max(TraceEventModel.sequence_no), 0) + 1).where(
                    TraceEventModel.run_id == run_id
                )
            ).scalar_one()
        return int(value)

    def append_event(self, event: TraceEventRecord) -> TraceEventRecord:
        """在序列锁保护下为事件分配 run 内序号并落库。

        在进程内共享序列锁内完成“算 sequence_no + 插入”这一原子操作，避免同一 run 内序号重复
        （详见模块 docstring 的并发说明）。返回的记录已回填分配到的 sequence_no。

        参数:
            event: 待写入的 trace event 记录（其 sequence_no 会被本方法覆盖分配）。

        返回:
            已回填 ``sequence_no`` 的 ``TraceEventRecord``。

        异常:
            TypeError: 如果 payload 无法 JSON 序列化。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            持有进程内序列锁；向 ``trace_events`` 表插入一行。
        """

        with self._sequence_lock:
            with self._session_factory.begin() as session:
                sequence_no = int(
                    session.execute(
                        select(func.coalesce(func.max(TraceEventModel.sequence_no), 0) + 1).where(
                            TraceEventModel.run_id == event.run_id
                        )
                    ).scalar_one()
                )
                assigned = replace(event, sequence_no=sequence_no)
                session.add(_event_model(assigned))
        return assigned

    def start_span(self, span: TraceSpanRecord) -> None:
        """写入一个处于开始状态的 span。

        参数:
            span: 待写入的 span 记录（通常尚无结束时间与时长）。

        返回:
            无。

        异常:
            TypeError: 如果 attributes 或 error 无法 JSON 序列化。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``trace_spans`` 表插入一行。
        """

        with self._session_factory.begin() as session:
            session.add(_span_model(span))

    def finish_span(
        self,
        span_id: str,
        status: str,
        ended_at: datetime | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """结束一个 span：写入状态、结束时间、时长与可选错误。

        结束时间缺省取当前 UTC 时间；时长由结束时间与该 span 的 started_at 之差（毫秒）计算。

        参数:
            span_id: 待结束的 span 标识。
            status: span 的结束状态。
            ended_at: 可选的结束时间；缺省为当前 UTC 时间。
            error: 可选的错误信息字典；存在时序列化为 JSON 存储。

        返回:
            无。

        异常:
            KeyError: 如果指定 span 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            更新 ``trace_spans`` 表中对应行的 status、ended_at、duration_ms 与 error_json。
        """

        finished_at = ended_at or datetime.now(timezone.utc)
        with self._session_factory.begin() as session:
            row = session.get(TraceSpanModel, span_id)
            if row is None:
                raise KeyError(span_id)
            duration_ms = int((finished_at - _from_text(row.started_at)).total_seconds() * 1000)
            row.status = status
            row.ended_at = _to_text(finished_at)
            row.duration_ms = duration_ms
            row.error_json = json.dumps(error, ensure_ascii=False) if error else None

    def list_events(
        self, trace_id: str = "", run_id: str = "", limit: int = 200
    ) -> list[TraceEventRecord]:
        """按 trace / run 过滤查询 trace event，按 run 与序号排序。

        trace_id 与 run_id 为空字符串时表示该维度不过滤；结果按 run_id、sequence_no、
        created_at 升序，便于按 run 顺序回放。

        参数:
            trace_id: 可选的 trace 过滤条件；空串表示不过滤。
            run_id: 可选的 run 过滤条件；空串表示不过滤。
            limit: 返回条数上限，必须大于 0，默认 200。

        返回:
            匹配的 trace event 列表；无匹配时为空列表。

        异常:
            ValueError: 如果 limit 小于 1。
            json.JSONDecodeError: 如果库中 payload_json 不是合法 JSON。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        if limit < 1:
            raise ValueError("limit must be greater than zero")
        statement = select(TraceEventModel)
        if trace_id:
            statement = statement.where(TraceEventModel.trace_id == trace_id)
        if run_id:
            statement = statement.where(TraceEventModel.run_id == run_id)
        with self._session_factory() as session:
            rows = (
                session.execute(
                    statement.order_by(
                        asc(TraceEventModel.run_id),
                        asc(TraceEventModel.sequence_no),
                        asc(TraceEventModel.created_at),
                    ).limit(limit)
                )
                .scalars()
                .all()
            )
        return [_event_from_model(row) for row in rows]

    def list_trace_summaries(self, limit: int = 100) -> list[dict[str, Any]]:
        """按 trace_id 聚合，返回最近的 trace 摘要列表。

        对 trace_events 按 trace_id 分组，聚合出 task_id、run_id、事件数、起止时间，按最近更新
        时间倒序返回。

        参数:
            limit: 返回条数上限，必须大于 0，默认 100。

        返回:
            trace 摘要字典列表（含 trace_id / task_id / run_id / event_count /
            started_at / updated_at）；无数据时为空列表。

        异常:
            ValueError: 如果 limit 小于 1。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        if limit < 1:
            raise ValueError("limit must be greater than zero")
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(
                        TraceEventModel.trace_id,
                        func.min(TraceEventModel.task_id).label("task_id"),
                        func.min(TraceEventModel.run_id).label("run_id"),
                        func.count().label("event_count"),
                        func.min(TraceEventModel.created_at).label("started_at"),
                        func.max(TraceEventModel.created_at).label("updated_at"),
                    )
                    .group_by(TraceEventModel.trace_id)
                    .order_by(desc("updated_at"))
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def get_trace_summary(self, trace_id: str) -> dict[str, Any]:
        """返回单个 trace 的聚合摘要。

        参数:
            trace_id: trace 标识。

        返回:
            该 trace 的摘要字典（含 trace_id / task_id / run_id / event_count /
            started_at / updated_at）。

        异常:
            KeyError: 如果该 trace 没有任何事件。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            row = (
                session.execute(
                    select(
                        TraceEventModel.trace_id,
                        func.min(TraceEventModel.task_id).label("task_id"),
                        func.min(TraceEventModel.run_id).label("run_id"),
                        func.count().label("event_count"),
                        func.min(TraceEventModel.created_at).label("started_at"),
                        func.max(TraceEventModel.created_at).label("updated_at"),
                    )
                    .where(TraceEventModel.trace_id == trace_id)
                    .group_by(TraceEventModel.trace_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise KeyError(trace_id)
        return dict(row)

    def list_spans(
        self, trace_id: str = "", run_id: str = "", limit: int = 200
    ) -> list[TraceSpanRecord]:
        """按 trace / run 过滤查询 trace span，按开始时间升序。

        trace_id 与 run_id 为空字符串时表示该维度不过滤。

        参数:
            trace_id: 可选的 trace 过滤条件；空串表示不过滤。
            run_id: 可选的 run 过滤条件；空串表示不过滤。
            limit: 返回条数上限，必须大于 0，默认 200。

        返回:
            匹配的 trace span 列表，按 ``started_at`` 升序；无匹配时为空列表。

        异常:
            ValueError: 如果 limit 小于 1。
            json.JSONDecodeError: 如果库中 attributes_json / error_json 不是合法 JSON。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        if limit < 1:
            raise ValueError("limit must be greater than zero")
        statement = select(TraceSpanModel)
        if trace_id:
            statement = statement.where(TraceSpanModel.trace_id == trace_id)
        if run_id:
            statement = statement.where(TraceSpanModel.run_id == run_id)
        with self._session_factory() as session:
            rows = (
                session.execute(statement.order_by(asc(TraceSpanModel.started_at)).limit(limit))
                .scalars()
                .all()
            )
        return [_span_from_model(row) for row in rows]

    def delete_by_task_ids(self, task_ids: list[str]) -> None:
        """删除一批任务下的 trace events 与 spans。

        参数:
            task_ids: 需要删除的任务标识符列表；为空时不执行任何操作。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            删除 ``trace_events`` 与 ``trace_spans`` 中的关联记录。
        """

        if not task_ids:
            return
        with self._session_factory.begin() as session:
            session.execute(
                delete(TraceEventModel).where(TraceEventModel.task_id.in_(tuple(task_ids)))
            )
            session.execute(
                delete(TraceSpanModel).where(TraceSpanModel.task_id.in_(tuple(task_ids)))
            )

    @classmethod
    def _lock_for_session_factory(cls, session_factory: sessionmaker) -> Lock:
        """返回同一 session_factory 共享的 trace sequence 分配锁。

        参数:
            session_factory: 主库 session 工厂；同一物理数据库对应的 session_factory
                实例会复用同一把进程内锁，从而保证跨 store 实例的序列号分配互斥。

        返回:
            进程内共享锁。

        异常:
            无。

        副作用:
            首次访问某 session_factory 时创建并登记一把锁。以 session_factory 对象
            本身作为字典键，可避免 id 复用风险，并在对象存活期间保持引用。
        """

        with cls._sequence_locks_guard:
            if session_factory not in cls._sequence_locks:
                cls._sequence_locks[session_factory] = Lock()
            return cls._sequence_locks[session_factory]


def _to_text(value: datetime) -> str:
    """将 datetime 转换为 UTC ISO 文本。

    参数:
        value: 待转换的 datetime（带或不带时区均可，会被转到 UTC）。

    返回:
        UTC 时区的 ISO 8601 文本。

    异常:
        无。

    副作用:
        无。
    """

    return value.astimezone(timezone.utc).isoformat()


def _event_model(event: TraceEventRecord) -> TraceEventModel:
    """将 trace event 记录转换为 ``TraceEventModel`` 行。

    参数:
        event: 待落库的 trace event 记录。

    返回:
        对应的 ``TraceEventModel``；payload 序列化为 JSON，created_at 转 UTC 文本。

    异常:
        TypeError: 如果 payload 无法 JSON 序列化。

    副作用:
        无。
    """

    return TraceEventModel(
        event_id=event.event_id,
        trace_id=event.trace_id,
        run_id=event.run_id,
        task_id=event.task_id,
        span_id=event.span_id,
        parent_span_id=event.parent_span_id,
        sequence_no=event.sequence_no,
        event_type=event.event_type,
        source=event.source,
        level=event.level,
        payload_json=json.dumps(event.payload, ensure_ascii=False),
        created_at=_to_text(event.created_at),
    )


def _event_from_model(row: TraceEventModel) -> TraceEventRecord:
    """将 ``TraceEventModel`` 行转换为 trace event 记录。

    可空外键列（span_id / parent_span_id）为 None 时回退为空字符串。

    参数:
        row: 查询得到的 ``TraceEventModel`` 行。

    返回:
        对应的 ``TraceEventRecord``。

    异常:
        json.JSONDecodeError: 如果 payload_json 不是合法 JSON。

    副作用:
        无。
    """

    return TraceEventRecord(
        trace_id=row.trace_id,
        run_id=row.run_id,
        task_id=row.task_id,
        event_type=row.event_type,
        source=row.source,
        payload=json.loads(row.payload_json),
        sequence_no=row.sequence_no,
        event_id=row.event_id,
        span_id=row.span_id or "",
        parent_span_id=row.parent_span_id or "",
        level=row.level,
        created_at=_from_text(row.created_at),
    )


def _span_model(span: TraceSpanRecord) -> TraceSpanModel:
    """将 trace span 记录转换为 ``TraceSpanModel`` 行。

    时间戳转 UTC 文本；ended_at 为空时存 None；attributes 与可选 error 序列化为 JSON。

    参数:
        span: 待落库的 trace span 记录。

    返回:
        对应的 ``TraceSpanModel``。

    异常:
        TypeError: 如果 attributes 或 error 无法 JSON 序列化。

    副作用:
        无。
    """

    return TraceSpanModel(
        span_id=span.span_id,
        trace_id=span.trace_id,
        run_id=span.run_id,
        task_id=span.task_id,
        parent_span_id=span.parent_span_id,
        name=span.name,
        kind=span.kind,
        status=span.status,
        started_at=_to_text(span.started_at),
        ended_at=_to_text(span.ended_at) if span.ended_at else None,
        duration_ms=span.duration_ms,
        attributes_json=json.dumps(span.attributes, ensure_ascii=False),
        error_json=json.dumps(span.error, ensure_ascii=False) if span.error else None,
    )


def _span_from_model(row: TraceSpanModel) -> TraceSpanRecord:
    """将 ``TraceSpanModel`` 行转换为 trace span 记录。

    可空的 parent_span_id 为 None 时回退为空字符串；ended_at / error_json 为空时对应字段为 None。

    参数:
        row: 查询得到的 ``TraceSpanModel`` 行。

    返回:
        对应的 ``TraceSpanRecord``。

    异常:
        json.JSONDecodeError: 如果 attributes_json / error_json 不是合法 JSON。

    副作用:
        无。
    """

    return TraceSpanRecord(
        row.span_id,
        row.trace_id,
        row.run_id,
        row.task_id,
        row.parent_span_id or "",
        row.name,
        row.kind,
        row.status,
        json.loads(row.attributes_json),
        _from_text(row.started_at),
        _from_text(row.ended_at) if row.ended_at else None,
        row.duration_ms,
        json.loads(row.error_json) if row.error_json else None,
    )
