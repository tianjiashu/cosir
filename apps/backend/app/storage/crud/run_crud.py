"""``runs`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``runs`` 单表的增删改查与 model↔record 转换。

职责边界：
    - 负责：run 状态读写与条件更新、
  ``RunModel``↔``RunRecord`` 转换。
- 不负责：Transport 命令接收与 Run 编排、跨表操作与会话
  编排（由 ``service/session/`` 负责）、业务规则。

依赖约定：构造时通过 ``main_db_factory()`` 取得主库共享 db 工厂，必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

import json

from sqlalchemy import asc, delete, select, update
from sqlalchemy.orm import Session as DbSession

from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.models import RunError, RunRecord
from app.models.enums.run_status import RunStatus
from app.models.run_options import RunOptions
from app.models.run_usage import RunUsage
from app.storage.model.run_model import RunModel
from app.storage.store_engines import main_db_factory


def _serialize_typed_json(value: object | None) -> str | None:
    """把可选的 typed JSON 字段（usage / error）序列化为列值。

    与 ``RunRecord.to_model`` 使用同一组序列化参数（排序键 + 拒绝 NaN），
    保证 CRUD 直接写列的内容仍可被 ``RunRecord.from_model`` 严格还原；
    ``None`` 表示该列不写（读取侧按缺失处理）。

    参数:
        value: typed JSON 字典或 ``None``。

    返回:
        JSON 文本；``value`` 为 ``None`` 时返回 ``None``。

    异常:
        TypeError: ``value`` 含无法 JSON 序列化的对象或 NaN。

    副作用:
        无。
    """

    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class RunCrud:
    """``runs`` 表的纯 CRUD。

    仅负责单表读写与 model↔record 转换，不承担跨表编排；所有方法通过共享主库
    db 工厂访问数据库。
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
            无（仅复用已初始化的主库 db 工厂）。
        """
        self._db_factory = main_db_factory()

    def save_execution(self, run_id: int, data: ReactExecutionState) -> None:
        """单一驱动者整体覆盖 Run 执行状态；生命周期仍由状态 service 条件迁移。"""
        with self._db_factory.begin() as db:
            db.execute(
                update(RunModel)
                .where(RunModel.id == run_id)
                .values(execution_data=data.model_dump(mode="json"))
            )

    def update_completed_result(
        self,
        run_id: int,
        *,
        final_output: str | None = None,
        end_reason: str | None = None,
        usage: RunUsage | None = None,
    ) -> RunRecord | None:
        """仅更新已完成 Run 的支线结果，不产生状态迁移；未命中返回 None。"""

        values: dict[str, object] = {}
        if final_output is not None:
            values["final_output"] = final_output
        if end_reason is not None:
            values["end_reason"] = end_reason
        if usage is not None:
            values["usage_json"] = _serialize_typed_json(usage)
        with self._db_factory.begin() as db:
            result = db.execute(
                update(RunModel)
                .where(
                    RunModel.id == run_id,
                    RunModel.status == RunStatus.COMPLETED.value,
                )
                .values(**values)
            )
            if not result.rowcount:
                return None
            db.flush()
            return self.get_in_db(db, run_id)

    def create(
        self,
        session_id: int,
        status: str,
        agent_id: str | None = None,
        model_config_id: str | None = None,
        context_window_total: int | None = None,
        options: RunOptions | None = None,
        usage: RunUsage | None = None,
        error: RunError | None = None,
        db: DbSession | None = None,
    ) -> RunRecord:
        """在调用方事务内创建 Run 记录；不发布事件、不写会话消息。"""
        if db is not None:
            return self._insert_and_flush(
                db,
                session_id=session_id,
                status=status,
                agent_id=agent_id,
                model_config_id=model_config_id,
                context_window_total=context_window_total,
                options=options,
                usage=usage,
                error=error,
            )
        with self._db_factory.begin() as managed_db:
            return self._insert_and_flush(
                managed_db,
                session_id=session_id,
                status=status,
                agent_id=agent_id,
                model_config_id=model_config_id,
                context_window_total=context_window_total,
                options=options,
                usage=usage,
                error=error,
            )

    @staticmethod
    def _insert_and_flush(
        db: DbSession,
        *,
        session_id: int,
        status: str,
        agent_id: str | None,
        model_config_id: str | None,
        context_window_total: int | None,
        options: RunOptions | None,
        usage: RunUsage | None,
        error: RunError | None,
    ) -> RunRecord:
        """在给定 db 内插入 run 行并 flush 取回自增 id。

        直接构造 ``RunModel``：``id`` / ``created_at`` / ``updated_at`` 由存储基类
        在写入时填充，本方法只负责业务列；执行进度从零计数开始。

        参数:
            db: 处于事务中的 SQLAlchemy session。
            session_id: 所属会话标识。
            status: 初始状态字符串。
            agent_id: agent 标识或 None。
            model_config_id: 厂商标识或 None。
            context_window_total: 本次 Run 使用的模型上下文窗口上限。
            options: 本次执行配置。
            usage: 初始 token 用量或 None。
            error: 初始结构化错误或 None。

        返回:
            由落库 model 映射得到的 ``RunRecord``。

        异常:
            TypeError: usage / error 不符合 typed JSON 契约。
            sqlalchemy.exc.SQLAlchemyError: 如果 flush 失败（如外键约束不满足）。

        副作用:
            向 db 追加一行 ``RunModel`` 并 flush。
        """
        model = RunModel(
            session_id=session_id,
            status=status,
            agent_id=agent_id,
            model_config_id=model_config_id,
            context_window_total=context_window_total,
            options=(options or RunOptions()).model_dump(mode="json"),
            execution_data=ReactExecutionState().model_dump(mode="json"),
            usage_json=_serialize_typed_json(usage),
            error_json=_serialize_typed_json(error),
        )

        db.add(model)
        db.flush()
        return RunRecord.from_model(model)

    def get(self, run_id: int) -> RunRecord:
        """按标识返回单个 run。

        参数:
            run_id: run 标识（整数 id）。

        返回:
            匹配的 ``RunRecord``。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 db。
        """
        with self._db_factory() as db:
            row: RunModel | None = db.get(RunModel, run_id)
            if row is None:
                raise KeyError(run_id)
            return RunRecord.from_model(row)

    def get_many(self, run_ids: list[int]) -> dict[int, RunRecord]:
        """批量读取已知 Run 标识，供聚合只读投影避免逐条查询。"""

        if not run_ids:
            return {}
        with self._db_factory() as db:
            rows = db.scalars(
                select(RunModel).where(RunModel.id.in_(run_ids))
            ).all()
        return {row.id: RunRecord.from_model(row) for row in rows}

    @staticmethod
    def get_in_db(db: DbSession, run_id: int) -> RunRecord:
        """在调用方事务 db 中读取单个 run。"""

        row: RunModel | None = db.get(RunModel, run_id)
        if row is None:
            raise KeyError(run_id)
        return RunRecord.from_model(row)

    @staticmethod
    def list_by_session_in_db(db: DbSession, session_id: int) -> list[RunRecord]:
        """在调用方事务中按创建顺序读取会话全部 run。"""

        rows = (
            db.execute(
                select(RunModel)
                .where(RunModel.session_id == session_id)
                .order_by(
                    asc(RunModel.created_at),
                    asc(RunModel.id),
                )
            )
            .scalars()
            .all()
        )
        return [RunRecord.from_model(row) for row in rows]

    def has_run_in_status(
        self,
        session_id: int,
        statuses: tuple[str, ...],
        db: DbSession,
    ) -> bool:
        """在给定事务内判断某会话是否存在处于指定状态集合的 run。

        纯存在性检查：仅投影 ``id`` 并 ``LIMIT 1``，不加载完整 run 记录，也不返回
        匹配行。供 service 层判断会话是否已有处于特定状态（如 running/suspended）的 run，
        避免为"仅判存在"而取出整行。

        参数:
            session_id: 会话标识（整数 id）。
            statuses: 待匹配的状态白名单。
            db: 外部事务 db；本方法不提交、不关闭该 db。

        返回:
            存在至少一个匹配 run 时返回 ``True``；无匹配时返回 ``False``。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            无（只读查询，不修改 db 状态）。
        """
        row_id = db.execute(
            select(RunModel.id)
            .where(RunModel.session_id == session_id)
            .where(RunModel.status.in_(statuses))
            .limit(1)
        ).scalar_one_or_none()
        return row_id is not None

    def list_by_session(self, session_id: int) -> list[RunRecord]:
        """列出某会话下的全部 run，按创建时间升序。

        参数:
            session_id: 会话标识（整数 id）。

        返回:
            该会话的 run 列表，按 ``created_at`` 再 ``id`` 升序；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 db。
        """
        with self._db_factory() as db:
            rows = (
                db.execute(
                    select(RunModel)
                    .where(RunModel.session_id == session_id)
                    .order_by(
                        asc(RunModel.created_at),
                        asc(RunModel.id),
                    )
                )
                .scalars()
                .all()
            )
        return [RunRecord.from_model(row) for row in rows]

    def list_recoverable(self) -> list[RunRecord]:
        """读取失去执行者的 running Run；waiting 保留待决请求。"""
        with self._db_factory() as db:
            rows = (
                db.execute(
                    select(RunModel)
                    .where(
                        RunModel.status == RunStatus.RUNNING.value
                    )
                    .order_by(
                        asc(RunModel.created_at),
                        asc(RunModel.id),
                    )
                )
                .scalars()
                .all()
            )
        return [RunRecord.from_model(row) for row in rows]

    def update_status_if_in(
        self,
        run_id: int,
        target_status: str,
        allowed_statuses: tuple[str, ...],
        end_reason: str | None = None,
        final_output: str | None = None,
        usage: RunUsage | None = None,
        error: RunError | None = None,
        db: DbSession | None = None,
        clear_terminal_fields: bool = False,
    ) -> RunRecord | None:
        """按来源状态白名单原子更新，传入外部 db 时加入用例事务。"""
        if db is not None:
            return self.update_status_if_in_db(
                db,
                run_id,
                target_status,
                allowed_statuses,
                end_reason,
                final_output,
                usage,
                error,
                clear_terminal_fields,
            )
        with self._db_factory.begin() as db:
            return self.update_status_if_in_db(
                db,
                run_id,
                target_status,
                allowed_statuses,
                end_reason,
                final_output,
                usage,
                error,
                clear_terminal_fields,
            )

    @staticmethod
    def update_status_if_in_db(
        db: DbSession,
        run_id: int,
        target_status: str,
        allowed_statuses: tuple[str, ...],
        end_reason: str | None = None,
        final_output: str | None = None,
        usage: RunUsage | None = None,
        error: RunError | None = None,
        clear_terminal_fields: bool = False,
    ) -> RunRecord | None:
        """事务内条件更新 Run 状态与结果字段；只返回真正命中的记录。执行状态只经 ``save_execution`` 写。"""

        values: dict[str, object] = {"status": target_status}
        if clear_terminal_fields:
            values.update(
                end_reason=None,
                final_output=None,
                usage_json=None,
                error_json=None,
            )
        if end_reason is not None:
            values["end_reason"] = end_reason
        if final_output is not None:
            values["final_output"] = final_output
        if usage is not None:
            values["usage_json"] = _serialize_typed_json(usage)
        if error is not None:
            values["error_json"] = _serialize_typed_json(error)
        result = db.execute(
            update(RunModel)
            .where(
                RunModel.id == run_id,
                RunModel.status.in_(allowed_statuses),
            )
            .values(**values)
        )
        if not result.rowcount:
            return None
        db.flush()
        return RunCrud.get_in_db(db, run_id)

    def list_ids_by_session_ids(self, session_ids: list[int]) -> list[int]:
        """返回一批会话下全部 run 的标识列表。

        只查 ``id`` 一列，用于跨表级联删除等只需 id 的场景。

        参数:
            session_ids: 会话标识列表（整数 id）；为空时直接返回空列表。

        返回:
            匹配的 run id 列表；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            session_ids 非空时打开一次主库只读 db。
        """
        if not session_ids:
            return []
        with self._db_factory() as db:
            return [
                row[0]
                for row in db.execute(
                    select(RunModel.id).where(
                        RunModel.session_id.in_(session_ids)
                    )
                ).all()
            ]

    def collect_run_ids_by_session_ids(self, db: DbSession, session_ids: list[int]) -> list[int]:
        """在调用方事务内返回一批会话下全部 run 的标识列表。

        与 ``list_ids_by_session_ids`` 语义相同，但复用调用方传入的事务 db，用于
        级联删除在单个共享事务内先收集 run 标识再删除，保证原子性。

        参数:
            db: 处于事务中的 SQLAlchemy session（本方法只读、不提交）。
            session_ids: 会话标识列表（整数 id）；为空时直接返回空列表。

        返回:
            匹配的 run id 列表；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            无（仅在该事务内执行一次只读查询）。
        """

        if not session_ids:
            return []
        return [
            row[0]
            for row in db.execute(
                select(RunModel.id).where(RunModel.session_id.in_(session_ids))
            ).all()
        ]

    def delete_by_ids(self, run_ids: list[int], db: DbSession | None = None) -> None:
        """按标识批量删除 run。

        参数:
            run_ids: 待删除的 run 标识列表（整数 id）；为空时不执行任何操作。
            db: 可选外部事务 db；传入时复用该事务不自行提交，为 None 时
                自开事务并自动提交。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            run_ids 为空时直接返回；对应行不存在时静默无操作。
        """
        if not run_ids:
            return
        if db is not None:
            db.execute(
                delete(RunModel).where(RunModel.id.in_(run_ids))
            )
            return
        with self._db_factory.begin() as db:
            db.execute(
                delete(RunModel).where(RunModel.id.in_(run_ids))
            )
