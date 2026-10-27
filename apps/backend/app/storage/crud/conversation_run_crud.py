"""``conversation_runs`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``conversation_runs`` 单表的增删改查与 model↔record 转换。

职责边界：
    - 负责：run 状态读写与条件更新、
  ``ConversationRunModel``↔``ConversationRunRecord`` 转换。
- 不负责：Transport 命令接收与 Run 编排、跨表操作与任务
  编排（由 ``service/task/`` 负责）、业务规则。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

import json

from sqlalchemy import asc, delete, select, update
from sqlalchemy.orm import Session

from app.models import ConversationRunError, ConversationRunRecord
from app.models.conversation_run_options import ConversationRunOptions
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.models.conversation_run_usage import ConversationRunUsage
from app.models.enums.conversation_run_status import ACTIVE_RUN_STATUSES, ConversationRunStatus
from app.storage.model.conversation_run_model import ConversationRunModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text


def _serialize_typed_json(value: object | None) -> str | None:
    """把可选的 typed JSON 字段（usage / error）序列化为列值。

    与 ``ConversationRunRecord.to_model`` 使用同一组序列化参数（排序键 + 拒绝 NaN），
    保证 CRUD 直接写列的内容仍可被 ``ConversationRunRecord.from_model`` 严格还原；
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


class ConversationRunCrud:
    """``conversation_runs`` 表的纯 CRUD。

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
            RuntimeError: 如果 ``init_storage`` 尚未调用（主库 session 工厂不可用）。

        副作用:
            无（仅复用已初始化的主库 session 工厂）。
        """
        self._session_factory = main_session_factory()

    def save_execution(self, run_id: int, data: ReactExecutionState) -> None:
        """单一驱动者整体覆盖 Run 执行状态；生命周期仍由状态 service 条件迁移。"""
        with self._session_factory.begin() as session:
            session.execute(
                update(ConversationRunModel)
                .where(ConversationRunModel.id == run_id)
                .values(execution_data=data.model_dump(mode="json"))
            )

    def update_completed_result(
        self,
        run_id: int,
        *,
        final_output: str | None = None,
        end_reason: str | None = None,
        usage: ConversationRunUsage | None = None,
    ) -> ConversationRunRecord | None:
        """仅更新已完成 Run 的支线结果，不产生状态迁移；未命中返回 None。"""

        values: dict[str, object] = {}
        if final_output is not None:
            values["final_output"] = final_output
        if end_reason is not None:
            values["end_reason"] = end_reason
        if usage is not None:
            values["usage_json"] = _serialize_typed_json(usage)
        with self._session_factory.begin() as session:
            result = session.execute(
                update(ConversationRunModel)
                .where(
                    ConversationRunModel.id == run_id,
                    ConversationRunModel.status == ConversationRunStatus.COMPLETED.value,
                )
                .values(**values)
            )
            if not result.rowcount:
                return None
            session.flush()
            return self.get_in_session(session, run_id)

    def create(
        self,
        task_id: int,
        status: str,
        agent_id: str | None = None,
        model_config_id: int | None = None,
        context_window_total: int | None = None,
        options: ConversationRunOptions | None = None,
        usage: ConversationRunUsage | None = None,
        error: ConversationRunError | None = None,
        session: Session | None = None,
    ) -> ConversationRunRecord:
        """在调用方事务内创建 Run 记录；不发布事件、不写会话消息。"""
        if session is not None:
            return self._insert_and_flush(
                session,
                task_id=task_id,
                status=status,
                agent_id=agent_id,
                model_config_id=model_config_id,
                context_window_total=context_window_total,
                options=options,
                usage=usage,
                error=error,
            )
        with self._session_factory.begin() as managed_session:
            return self._insert_and_flush(
                managed_session,
                task_id=task_id,
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
        session: Session,
        *,
        task_id: int,
        status: str,
        agent_id: str | None,
        model_config_id: int | None,
        context_window_total: int | None,
        options: ConversationRunOptions | None,
        usage: ConversationRunUsage | None,
        error: ConversationRunError | None,
    ) -> ConversationRunRecord:
        """在给定 session 内插入 run 行并 flush 取回自增 id。

        直接构造 ``ConversationRunModel``：``id`` / ``created_at`` / ``updated_at`` 由存储基类
        在写入时填充，本方法只负责业务列；执行进度从零计数开始。

        参数:
            session: 处于事务中的 SQLAlchemy session。
            task_id: 所属任务标识。
            status: 初始状态字符串。
            agent_id: agent 标识或 None。
            model_config_id: 厂商标识或 None。
            context_window_total: 本次 Run 使用的模型上下文窗口上限。
            options: 本次执行配置。
            usage: 初始 token 用量或 None。
            error: 初始结构化错误或 None。

        返回:
            由落库 model 映射得到的 ``ConversationRunRecord``。

        异常:
            TypeError: usage / error 不符合 typed JSON 契约。
            sqlalchemy.exc.SQLAlchemyError: 如果 flush 失败（如外键约束不满足）。

        副作用:
            向 session 追加一行 ``ConversationRunModel`` 并 flush。
        """
        model = ConversationRunModel(
            task_id=task_id,
            status=status,
            agent_id=agent_id,
            model_config_id=model_config_id,
            context_window_total=context_window_total,
            options=(options or ConversationRunOptions()).model_dump(mode="json"),
            execution_data=ReactExecutionState().model_dump(mode="json"),
            usage_json=_serialize_typed_json(usage),
            error_json=_serialize_typed_json(error),
        )

        session.add(model)
        session.flush()
        return ConversationRunRecord.from_model(model)

    def get(self, run_id: int) -> ConversationRunRecord:
        """按标识返回单个 run。

        参数:
            run_id: run 标识（整数 id）。

        返回:
            匹配的 ``ConversationRunRecord``。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        with self._session_factory() as session:
            row: ConversationRunModel | None = session.get(ConversationRunModel, run_id)
            if row is None:
                raise KeyError(run_id)
            return ConversationRunRecord.from_model(row)

    def get_many(self, run_ids: list[int]) -> dict[int, ConversationRunRecord]:
        """批量读取已知 Run 标识，供聚合只读投影避免逐条查询。"""

        if not run_ids:
            return {}
        with self._session_factory() as session:
            rows = session.scalars(
                select(ConversationRunModel).where(ConversationRunModel.id.in_(run_ids))
            ).all()
        return {row.id: ConversationRunRecord.from_model(row) for row in rows}

    @staticmethod
    def get_in_session(session: Session, run_id: int) -> ConversationRunRecord:
        """在调用方事务 session 中读取单个 run。"""

        row: ConversationRunModel | None = session.get(ConversationRunModel, run_id)
        if row is None:
            raise KeyError(run_id)
        return ConversationRunRecord.from_model(row)

    @staticmethod
    def list_by_task_in_session(session: Session, task_id: int) -> list[ConversationRunRecord]:
        """在调用方事务中按创建顺序读取任务全部 run。"""

        rows = (
            session.execute(
                select(ConversationRunModel)
                .where(ConversationRunModel.task_id == task_id)
                .order_by(
                    asc(ConversationRunModel.created_at),
                    asc(ConversationRunModel.id),
                )
            )
            .scalars()
            .all()
        )
        return [ConversationRunRecord.from_model(row) for row in rows]

    @staticmethod
    def clone_for_task(
        session: Session,
        source: ConversationRunRecord,
        target_task_id: int,
    ) -> ConversationRunRecord:
        """在调用方事务中复制历史 Run；新 Run 的执行进度从初始状态开始。"""

        model = ConversationRunModel(
            task_id=target_task_id,
            agent_id=source.agent_id,
            model_config_id=source.model_config_id,
            context_window_total=source.context_window_total,
            end_reason=source.end_reason,
            final_output=source.final_output,
            options=source.options.model_dump(mode="json"),
            execution_data=ReactExecutionState().model_dump(mode="json"),
            usage_json=_serialize_typed_json(source.usage),
            error_json=_serialize_typed_json(source.error),
            status=source.status,
            created_at=to_text(source.created_at),
            updated_at=to_text(source.updated_at),
        )
        session.add(model)
        session.flush()
        return ConversationRunRecord.from_model(model)

    def has_run_in_status(
        self,
        task_id: int,
        statuses: tuple[str, ...],
        session: Session,
    ) -> bool:
        """在给定事务内判断某任务是否存在处于指定状态集合的 run。

        纯存在性检查：仅投影 ``id`` 并 ``LIMIT 1``，不加载完整 run 记录，也不返回
        匹配行。供 service 层判断任务是否已有处于特定状态（如 running/suspended）的 run，
        避免为"仅判存在"而取出整行。

        参数:
            task_id: 任务标识（整数 id）。
            statuses: 待匹配的状态白名单。
            session: 外部事务 session；本方法不提交、不关闭该 session。

        返回:
            存在至少一个匹配 run 时返回 ``True``；无匹配时返回 ``False``。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            无（只读查询，不修改 session 状态）。
        """
        row_id = session.execute(
            select(ConversationRunModel.id)
            .where(ConversationRunModel.task_id == task_id)
            .where(ConversationRunModel.status.in_(statuses))
            .limit(1)
        ).scalar_one_or_none()
        return row_id is not None

    def list_by_task(self, task_id: int) -> list[ConversationRunRecord]:
        """列出某任务下的全部 run，按创建时间升序。

        参数:
            task_id: 任务标识（整数 id）。

        返回:
            该任务的 run 列表，按 ``created_at`` 再 ``id`` 升序；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(ConversationRunModel)
                    .where(ConversationRunModel.task_id == task_id)
                    .order_by(
                        asc(ConversationRunModel.created_at),
                        asc(ConversationRunModel.id),
                    )
                )
                .scalars()
                .all()
            )
        return [ConversationRunRecord.from_model(row) for row in rows]

    def list_recoverable(self) -> list[ConversationRunRecord]:
        """读取失去执行者的 running Run；waiting 保留待决请求。"""
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(ConversationRunModel)
                    .where(
                        ConversationRunModel.status == ConversationRunStatus.RUNNING.value
                    )
                    .order_by(
                        asc(ConversationRunModel.created_at),
                        asc(ConversationRunModel.id),
                    )
                )
                .scalars()
                .all()
            )
        return [ConversationRunRecord.from_model(row) for row in rows]

    def has_active_for_task(self, task_id: int) -> bool:
        """判断 Task 是否存在活跃（running 或 suspended）Run。"""

        with self._session_factory() as session:
            row_id = session.execute(
                select(ConversationRunModel.id)
                .where(ConversationRunModel.task_id == task_id)
                .where(
                    ConversationRunModel.status.in_(ACTIVE_RUN_STATUSES)
                )
                .limit(1)
            ).scalar_one_or_none()
        return row_id is not None

    def update_status_if_in(
        self,
        run_id: int,
        target_status: str,
        allowed_statuses: tuple[str, ...],
        end_reason: str | None = None,
        final_output: str | None = None,
        usage: ConversationRunUsage | None = None,
        error: ConversationRunError | None = None,
        session: Session | None = None,
        clear_terminal_fields: bool = False,
    ) -> ConversationRunRecord | None:
        """按来源状态白名单原子更新，传入外部 Session 时加入用例事务。"""
        if session is not None:
            return self.update_status_if_in_session(
                session,
                run_id,
                target_status,
                allowed_statuses,
                end_reason,
                final_output,
                usage,
                error,
                clear_terminal_fields,
            )
        with self._session_factory.begin() as session:
            return self.update_status_if_in_session(
                session,
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
    def update_status_if_in_session(
        session: Session,
        run_id: int,
        target_status: str,
        allowed_statuses: tuple[str, ...],
        end_reason: str | None = None,
        final_output: str | None = None,
        usage: ConversationRunUsage | None = None,
        error: ConversationRunError | None = None,
        clear_terminal_fields: bool = False,
    ) -> ConversationRunRecord | None:
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
        result = session.execute(
            update(ConversationRunModel)
            .where(
                ConversationRunModel.id == run_id,
                ConversationRunModel.status.in_(allowed_statuses),
            )
            .values(**values)
        )
        if not result.rowcount:
            return None
        session.flush()
        return ConversationRunCrud.get_in_session(session, run_id)

    def list_ids_by_task_ids(self, task_ids: list[int]) -> list[int]:
        """返回一批任务下全部 run 的标识列表。

        只查 ``id`` 一列，用于跨表级联删除等只需 id 的场景。

        参数:
            task_ids: 任务标识列表（整数 id）；为空时直接返回空列表。

        返回:
            匹配的 run id 列表；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            task_ids 非空时打开一次主库只读 session。
        """
        if not task_ids:
            return []
        with self._session_factory() as session:
            return [
                row[0]
                for row in session.execute(
                    select(ConversationRunModel.id).where(
                        ConversationRunModel.task_id.in_(task_ids)
                    )
                ).all()
            ]

    def collect_run_ids_by_task_ids(self, session: Session, task_ids: list[int]) -> list[int]:
        """在调用方事务内返回一批任务下全部 run 的标识列表。

        与 ``list_ids_by_task_ids`` 语义相同，但复用调用方传入的事务 session，用于
        级联删除在单个共享事务内先收集 run 标识再删除，保证原子性。

        参数:
            session: 处于事务中的 SQLAlchemy session（本方法只读、不提交）。
            task_ids: 任务标识列表（整数 id）；为空时直接返回空列表。

        返回:
            匹配的 run id 列表；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            无（仅在该事务内执行一次只读查询）。
        """

        if not task_ids:
            return []
        return [
            row[0]
            for row in session.execute(
                select(ConversationRunModel.id).where(ConversationRunModel.task_id.in_(task_ids))
            ).all()
        ]

    def delete_by_ids(self, run_ids: list[int], session: Session | None = None) -> None:
        """按标识批量删除 run。

        参数:
            run_ids: 待删除的 run 标识列表（整数 id）；为空时不执行任何操作。
            session: 可选外部事务 session；传入时复用该事务不自行提交，为 None 时
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
        if session is not None:
            session.execute(
                delete(ConversationRunModel).where(ConversationRunModel.id.in_(run_ids))
            )
            return
        with self._session_factory.begin() as session:
            session.execute(
                delete(ConversationRunModel).where(ConversationRunModel.id.in_(run_ids))
            )
