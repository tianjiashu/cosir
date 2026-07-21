"""``durable_runs`` 表的纯 CRUD 数据访问层（持久化运行状态）。

单一职责：只提供 ``durable_runs`` 单表的读写与 model↔record 转换。durable run 记录一次
turn 执行的持久化运行状态（状态、等待原因、活跃 step / wait、中断原因等），用于进程重启后
恢复与断点续跑。

职责边界：
- 负责：run 单表读写、``DurableRunModel``↔``RunRecord`` 转换、按 turn 幂等创建。
- 不负责：运行时状态推进与恢复编排（由 ``core/runtime`` / ``core/runs`` 负责）、跨表级联。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

import logging
from collections.abc import Sequence
from uuid import uuid4

from sqlalchemy import asc, delete, select, update

from app.models import RunRecord
from app.storage.model.durable_model import DurableRunModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import utc_now, to_text, from_text

_LOGGER = logging.getLogger("coding_agent.backend")


class DurableRunStore:
    """``durable_runs`` 表的纯 CRUD。

    仅负责持久化运行状态的单表读写与 model↔record 转换，不承担运行时编排；所有方法通过共享主库
    session 工厂访问数据库。
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
            无（仅复用已初始化的主库 session 工厂，不创建 / 释放共享引擎）。
        """

        self._session_factory = main_session_factory()

    def create_for_turn(
            self,
            task_id: str,
            turn_id: str,
            status: str,
            thread_id: str | None = None,
    ) -> RunRecord:
        """为某个 turn 创建 run；若该 turn 已有 run 则直接返回既有 run（幂等）。

        以 turn 维度保证唯一：先按 turn 查询，命中则返回既有记录，否则新建。``thread_id``
        缺省时自动生成 UUID4；创建 / 更新时间以当前 UTC 时间填充。

        参数:
            task_id: 所属任务标识。
            turn_id: 所属 turn 标识（幂等键）。
            status: run 初始状态。
            thread_id: 可选的稳定工具执行线程标识；缺省时自动生成。

        返回:
            新建或既有的 ``RunRecord``。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果持久化失败。

        副作用:
            当该 turn 尚无 run 时，向 ``durable_runs`` 表插入一行。
        """

        existing = self.get_by_turn(turn_id)
        if existing is not None:
            return existing
        now = utc_now()
        run = RunRecord(
            task_id,
            turn_id,
            thread_id or str(uuid4()),
            status,
            None,
            None,
            None,
            None,
            now,
            now,
        )
        with self._session_factory.begin() as session:
            session.add(self._run_model(run))
        return run

    def get(self, run_id: str) -> RunRecord:
        """按标识返回单个 run。

        参数:
            run_id: run 标识。

        返回:
            匹配的 ``RunRecord``。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            row = session.get(DurableRunModel, run_id)
        if row is None:
            raise KeyError(run_id)
        return self._run_from_model(row)

    def get_by_turn(self, turn_id: str) -> RunRecord | None:
        """返回某 turn 对应的 run（不存在时返回 None）。

        参数:
            turn_id: turn 标识。

        返回:
            匹配的 ``RunRecord``；该 turn 尚无 run 时返回 None。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            row = session.execute(
                select(DurableRunModel).where(DurableRunModel.turn_id == turn_id)
            ).scalar_one_or_none()
        return self._run_from_model(row) if row is not None else None

    def list_by_task(self, task_id: str) -> list[RunRecord]:
        """列出某任务下的全部 run，按创建时间升序。

        参数:
            task_id: 任务标识。

        返回:
            该任务的 run 列表，按 ``created_at`` 再 ``run_id`` 升序；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(DurableRunModel)
                    .where(DurableRunModel.task_id == task_id)
                    .order_by(asc(DurableRunModel.created_at), asc(DurableRunModel.run_id))
                )
                .scalars()
                .all()
            )
        return [self._run_from_model(row) for row in rows]

    def delete_by_task_ids(self, task_ids: Sequence[str]) -> list[str]:
        """批量删除一批任务下的全部 run，返回被删除的 run 标识。

        在同一事务中先查出待删 run_id，再批量删除；无论成功失败都会写运行日志（失败时记
        exception 并重新抛出，成功时记删除数量）。

        参数:
            task_ids: 任务标识序列；为空时直接返回空列表。

        返回:
            被删除的 run_id 列表；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询或删除失败（异常会先记日志再向上抛出）。

        副作用:
            从 ``durable_runs`` 表删除匹配行；写入 info / exception 运行日志。
        """

        if not task_ids:
            return []
        run_ids: list[str] = []
        try:
            with self._session_factory.begin() as session:
                run_ids = [
                    row[0]
                    for row in session.execute(
                        select(DurableRunModel.run_id).where(
                            DurableRunModel.task_id.in_(tuple(task_ids))
                        )
                    ).all()
                ]
                if run_ids:
                    session.execute(
                        delete(DurableRunModel).where(DurableRunModel.run_id.in_(run_ids))
                    )
        except Exception:
            _LOGGER.exception(
                "durable_runs_delete_failed",
                extra={
                    "msg": "durable runs delete failed",
                    "data": {"task_count": len(task_ids), "operation": "delete_by_task_ids"},
                },
            )
            raise
        _LOGGER.info(
            "durable_runs_deleted",
            extra={
                "msg": "durable runs deleted",
                "data": {"task_count": len(task_ids), "run_count": len(run_ids)},
            },
        )
        return run_ids

    def mark_status(
            self,
            run_id: str,
            status: str,
            wait_reason: str | None = None,
            active_step_id: str | None = None,
            active_wait_id: str | None = None,
            interruption_reason: str | None = None,
    ) -> RunRecord:
        """更新 run 的状态及相关运行字段。

        以单条 UPDATE 覆盖 status 与等待 / 活跃 / 中断相关字段，并刷新 ``updated_at``。注意：
        未显式传入的可选字段会被写为 None（即“清空”语义），调用方应一次性传入完整的当前状态。

        参数:
            run_id: run 标识。
            status: 新的 run 状态。
            wait_reason: 可选的等待原因；缺省写为 None。
            active_step_id: 可选的当前活跃 step 标识；缺省写为 None。
            active_wait_id: 可选的当前活跃 wait 标识；缺省写为 None。
            interruption_reason: 可选的中断原因；缺省写为 None。

        返回:
            更新后的 ``RunRecord``。

        异常:
            KeyError: 如果指定 run 不存在（更新影响行数不为 1）。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            更新 ``durable_runs`` 表中对应行的状态字段与 updated_at。
        """

        with self._session_factory.begin() as session:
            result = session.execute(
                update(DurableRunModel)
                .where(DurableRunModel.run_id == run_id)
                .values(
                    status=status,
                    wait_reason=wait_reason,
                    active_step_id=active_step_id,
                    active_wait_id=active_wait_id,
                    interruption_reason=interruption_reason,
                    updated_at=to_text(utc_now()),
                )
            )
        if result.rowcount != 1:
            raise KeyError(run_id)
        return self.get(run_id)

    def _run_from_model(self, row: DurableRunModel) -> RunRecord:
        """把 ``DurableRunModel`` ORM 行转换为业务 ``RunRecord``。

        转换过程把库中存储的文本时间戳还原为 datetime。

        参数:
            row: 查询得到的 ``DurableRunModel`` 行。

        返回:
            对应的 ``RunRecord``。

        异常:
            无。

        副作用:
            无。
        """

        return RunRecord(
            row.task_id,
            row.turn_id,
            row.thread_id,
            row.status,
            row.wait_reason,
            row.active_step_id,
            row.active_wait_id,
            row.interruption_reason,
            from_text(row.created_at),
            from_text(row.updated_at),
        )

    def _run_model(self, run: RunRecord) -> DurableRunModel:
        """把业务 ``RunRecord`` 转换为 ``DurableRunModel`` ORM 行。

        转换过程把 datetime 序列化为库中存储的文本时间戳。

        参数:
            run: 待落库的 ``RunRecord``。

        返回:
            对应的 ``DurableRunModel``。

        异常:
            无。

        副作用:
            无。
        """

        return DurableRunModel(
            task_id=run.task_id,
            turn_id=run.turn_id,
            thread_id=run.thread_id,
            status=run.status,
            wait_reason=run.wait_reason,
            active_step_id=run.active_step_id,
            active_wait_id=run.active_wait_id,
            interruption_reason=run.interruption_reason,
            created_at=to_text(run.created_at),
            updated_at=to_text(run.updated_at),
        )
