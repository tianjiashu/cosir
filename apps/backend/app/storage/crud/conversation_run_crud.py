"""``conversation_runs`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``conversation_runs`` 单表的增删改查与 model↔record 转换。

职责边界：
    - 负责：run 状态读写与条件更新、
  ``ConversationRunModel``↔``ConversationRunRecord`` 转换。
- 不负责：Transport 命令幂等占用（见 ``ConversationCommandCrud``）、跨表操作与任务
  编排（由 ``service/task/`` 负责）、业务规则。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

import copy
import json
from uuid import uuid4

from sqlalchemy import asc, delete, desc, select, update
from sqlalchemy.orm import Session

from app.models import ConversationRunError, ConversationRunExtra, ConversationRunRecord
from app.models.conversation_run_usage import ConversationRunUsage
from app.models.enums.conversation_run_status import ConversationRunStatus
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

    def create(
        self,
        task_id: int,
        input_text: str,
        status: str = ConversationRunStatus.PENDING.value,
        agent_id: str | None = None,
        provider_id: int | None = None,
        model_name: str | None = None,
        image_paths: list[str] | None = None,
        reasoning_effort: str | None = None,
        extra: ConversationRunExtra | None = None,
        usage: ConversationRunUsage | None = None,
        error: ConversationRunError | None = None,
        session: Session | None = None,
    ) -> ConversationRunRecord:
        """新建一条 run 记录并落库。

        主键 ``id`` 由存储引擎自增分配，即本次运行的 run 标识。

        参数:
            task_id: 所属任务标识（整数 id）。
            input_text: 本次运行的输入文本；不能为空白。
            status: 初始状态，默认 ``pending``。
            agent_id: 可选，本次运行绑定的 agent 标识。
            provider_id: 可选，模型归属厂商标识（指向 ``providers.id``）；None 表示未指定。
            model_name: 可选，模型路由名。
            image_paths: 可选，本次输入的图片路径列表（供多模态通道）。
            reasoning_effort: 可选，思考努力等级；None 表示未指定。
            extra: 可选，运行期附加结构化数据。
            usage: 可选，符合六键契约的运行 token 用量。
            error: 可选，结构化运行错误。
            session: 可选，外部事务 session；传入时复用该事务不自行提交，
                None 时自行开启并提交事务。

        返回:
            落库成功的 ``ConversationRunRecord``（含自增分配的 id）。

        异常:
            ValueError: 如果 input_text 去除首尾空白后为空。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``conversation_runs`` 表插入一行。
        """
        if not input_text.strip() and not image_paths:
            raise ValueError("input_text must be a non-empty string")

        if session is not None:
            return self._insert_and_flush(
                session,
                task_id=task_id,
                input_text=input_text,
                status=status,
                agent_id=agent_id,
                provider_id=provider_id,
                model_name=model_name,
                image_paths=image_paths,
                reasoning_effort=reasoning_effort,
                extra=extra,
                usage=usage,
                error=error,
            )
        with self._session_factory.begin() as managed_session:
            return self._insert_and_flush(
                managed_session,
                task_id=task_id,
                input_text=input_text,
                status=status,
                agent_id=agent_id,
                provider_id=provider_id,
                model_name=model_name,
                image_paths=image_paths,
                reasoning_effort=reasoning_effort,
                extra=extra,
                usage=usage,
                error=error,
            )

    @staticmethod
    def _insert_and_flush(
        session: Session,
        *,
        task_id: int,
        input_text: str,
        status: str,
        agent_id: str | None,
        provider_id: int | None,
        model_name: str | None,
        image_paths: list[str] | None,
        reasoning_effort: str | None,
        extra: ConversationRunExtra | None,
        usage: ConversationRunUsage | None,
        error: ConversationRunError | None,
    ) -> ConversationRunRecord:
        """在给定 session 内插入 run 行并 flush 取回自增 id。

        直接构造 ``ConversationRunModel``：``id`` / ``created_at`` / ``updated_at`` 由存储基类
        在写入时填充，本方法只负责业务列；``checkpoint_thread_id`` 是本次执行的 LangGraph
        身份，每条新 Run 都重新生成。

        参数:
            session: 处于事务中的 SQLAlchemy session。
            task_id: 所属任务标识。
            input_text: 输入文本（调用方已校验非空）。
            status: 初始状态字符串；空值回退 ``pending``。
            agent_id: agent 标识或 None。
            provider_id: 厂商标识或 None。
            model_name: 模型名或 None。
            image_paths: 图片路径列表或 None。
            reasoning_effort: 思考努力等级或 None。
            extra: 附加结构化数据或 None。
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
            input_text=input_text,
            status=status or ConversationRunStatus.PENDING.value,
            # 每条新 Run 必须拿到全新的 checkpoint 身份，不能复用任何已读到的历史身份。
            checkpoint_thread_id=str(uuid4()),
            agent_id=agent_id,
            provider_id=provider_id,
            model_name=model_name,
            image_paths=image_paths,
            reasoning_effort=reasoning_effort,
            extra=extra.to_dict() if extra is not None else None,
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
        """在调用方事务中静默复制历史 run，并为 fork 生成独立 checkpoint 身份。"""

        model = ConversationRunModel(
            task_id=target_task_id,
            input_text=source.input_text,
            agent_id=source.agent_id,
            provider_id=source.provider_id,
            model_name=source.model_name,
            image_paths=copy.deepcopy(source.image_paths),
            reasoning_effort=source.reasoning_effort,
            end_reason=source.end_reason,
            final_output=source.final_output,
            extra=(source.extra.to_dict() if source.extra is not None else None),
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
        匹配行。供 service 层判断任务是否已有处于特定状态（如 pending/running）的 run，
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

    def get_latest_by_task(self, task_id: int) -> ConversationRunRecord | None:
        """返回某任务下创建时间最新的 run（单条查询）。

        仅投影 ``created_at`` 与 ``id`` 的倒序第一条，避免为取最新 run 而加载全部
        run 列表。任务无任何 run 时返回 ``None``，不抛异常，由调用方决定后续行为。

        参数:
            task_id: 任务标识（整数 id）。

        返回:
            该任务下 ``created_at`` 再 ``id`` 倒序的第一条 ``ConversationRunRecord``；
            无匹配 run 时返回 ``None``。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            row: ConversationRunModel | None = (
                session.execute(
                    select(ConversationRunModel)
                    .where(ConversationRunModel.task_id == task_id)
                    .order_by(
                        desc(ConversationRunModel.created_at),
                        desc(ConversationRunModel.id),
                    )
                    .limit(1)
                )
                .scalars()
                .first()
            )
        if row is None:
            return None
        return ConversationRunRecord.from_model(row)

    def list_recoverable(self) -> list[ConversationRunRecord]:
        """返回进程重启后仍需恢复的 pending/running 运行。

        参数:
            无。

        返回:
            状态为 ``pending`` 或 ``running`` 的 run 列表，按 ``created_at`` 再 ``id``
            升序；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(ConversationRunModel)
                    .where(
                        ConversationRunModel.status.in_(
                            (
                                ConversationRunStatus.PENDING.value,
                                ConversationRunStatus.RUNNING.value,
                            )
                        )
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
        """Return whether the task has a canonical pending or running Run."""

        with self._session_factory() as session:
            row_id = session.execute(
                select(ConversationRunModel.id)
                .where(ConversationRunModel.task_id == task_id)
                .where(
                    ConversationRunModel.status.in_(
                        (
                            ConversationRunStatus.PENDING.value,
                            ConversationRunStatus.RUNNING.value,
                        )
                    )
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
        """以乐观锁方式把 run 更新为目标状态，仅当其当前状态在允许集合内。

        只负责 ``WHERE status IN (allowed_statuses)`` 条件下的原子更新，用于避免并发收尾
        请求改写历史终态；目标状态、允许的前置状态集合与伴随字段由调用方（service 层）
        决定。``clear_terminal_fields`` 为真时额外清空上一轮的四个终态字段（详见
        ``update_status_if_in_session``）；本方法不改动 ``checkpoint_thread_id``。

        参数:
            run_id: run 标识（整数 id）。
            target_status: 期望写入的状态字符串。
            allowed_statuses: 允许执行更新的前置状态白名单；当前状态不在此集合时
                不做任何修改并返回 None。
            end_reason: 可选，更新时一并写入的终态原因；为 None 时不修改该列。
            final_output: 可选，更新时一并写入的最终回答文本；为 None 时不修改该列。
            usage: 可选，更新时一并写入的 token 用量；为 None 时不修改该列。
            error: 可选，更新时一并写入的结构化错误；为 None 时不修改该列。
            session: 可选外部事务 session；传入时复用且不自行提交。
            clear_terminal_fields: 是否清空上一轮的四个终态字段（默认 False）。

        返回:
            更新成功时返回更新后的 ``ConversationRunRecord``；当前状态不在允许集合内时
            返回 None。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。注意：run 不存在时不会抛 KeyError，
            而是按零行更新静默返回 None（需判存在性时调用方应先 ``get``）。

        副作用:
            条件满足时更新对应行的 ``status``、``updated_at`` 与可选的伴随字段；不改动
            ``checkpoint_thread_id``。
        """
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

    def reset_for_edit(
        self,
        run_id: int,
        input_text: str,
        checkpoint_thread_id: str,
        allowed_statuses: tuple[str, ...],
        session: Session | None = None,
        provider_id: int | None = None,
        model_name: str | None = None,
        image_paths: list[str] | None = None,
        reasoning_effort: str | None = None,
        extra: ConversationRunExtra | None = None,
    ) -> ConversationRunRecord | None:
        """原子替换一个非活动 run 的输入与执行基线。"""

        if session is not None:
            return self.reset_for_edit_in_session(
                session,
                run_id,
                input_text,
                checkpoint_thread_id,
                allowed_statuses,
                provider_id,
                model_name,
                image_paths,
                reasoning_effort,
                extra,
            )
        with self._session_factory.begin() as managed_session:
            return self.reset_for_edit_in_session(
                managed_session,
                run_id,
                input_text,
                checkpoint_thread_id,
                allowed_statuses,
                provider_id,
                model_name,
                image_paths,
                reasoning_effort,
                extra,
            )

    @staticmethod
    def reset_for_edit_in_session(
        session: Session,
        run_id: int,
        input_text: str,
        checkpoint_thread_id: str,
        allowed_statuses: tuple[str, ...],
        provider_id: int | None = None,
        model_name: str | None = None,
        image_paths: list[str] | None = None,
        reasoning_effort: str | None = None,
        extra: ConversationRunExtra | None = None,
    ) -> ConversationRunRecord | None:
        """在外部事务中把 run 重置为待执行，并清空旧输出。"""

        result = session.execute(
            update(ConversationRunModel)
            .where(
                ConversationRunModel.id == run_id,
                ConversationRunModel.status.in_(allowed_statuses),
            )
            .values(
                input_text=input_text,
                checkpoint_thread_id=checkpoint_thread_id,
                status=ConversationRunStatus.PENDING.value,
                provider_id=provider_id,
                model_name=model_name,
                image_paths=image_paths,
                reasoning_effort=reasoning_effort,
                extra=extra.to_dict() if extra is not None else None,
                end_reason=None,
                final_output=None,
                usage_json=None,
                error_json=None,
            )
        )
        if not result.rowcount:
            return None
        session.flush()
        return ConversationRunCrud.get_in_session(session, run_id)

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
        """在给定事务中按状态白名单原子更新 run。

        除通用的状态白名单更新外，``clear_terminal_fields`` 为真时把上一轮的四个终态字段
        显式清空：给 ``end_reason`` 等可选参数传 ``None`` 只表示「不修改该列」，表达不了
        「清空」，因此清空必须是一个独立开关。

        本方法**不**改动 ``checkpoint_thread_id``。``resume`` 执行模式下 workflow 会以
        ``input_state=None`` 让 LangGraph 从该线程的**既有 checkpoint** 继续，因此续跑
        必须复用原线程；一旦轮换，续跑就会落到一个没有任何 checkpoint 的空线程上，续跑
        语义直接失效。线程的轮换只发生在「以新输入重新执行」的路径（``reset_for_edit``）。

        参数:
            session: 处于事务中的 SQLAlchemy session（本方法不提交）。
            run_id: run 标识（整数 id）。
            target_status: 期望写入的状态字符串。
            allowed_statuses: 允许执行更新的前置状态白名单；当前状态不在此集合时不做
                任何修改并返回 None。
            end_reason: 可选，更新时一并写入的终态原因；为 None 时不修改该列。
            final_output: 可选，更新时一并写入的最终回答文本；为 None 时不修改该列。
            usage: 可选，更新时一并写入的 token 用量；为 None 时不修改该列。
            error: 可选，更新时一并写入的结构化错误；为 None 时不修改该列。
            clear_terminal_fields: 是否清空上一轮的四个终态字段（默认 False）。为真时
                先清空，随后仍由非 None 的入参覆盖。

        返回:
            更新成功时返回更新后的 ``ConversationRunRecord``；当前状态不在允许集合内时
            返回 None。

        异常:
            TypeError: usage / error 不符合 typed JSON 契约。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            条件满足时更新对应行的 ``status``、``updated_at`` 与可选的伴随字段；不改动
            ``checkpoint_thread_id``。
        """

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

    def collect_checkpoint_threads_by_task_ids(
        self, session: Session, task_ids: list[int]
    ) -> set[str]:
        """在调用方事务内收集一批任务对应的 checkpoint thread id 集合。

        参数:
            session: 处于事务中的 SQLAlchemy session（本方法只读、不提交）。
            task_ids: 任务标识列表（整数 id）；为空时直接返回空集合。

        返回:
            这些任务下 run 的 ``checkpoint_thread_id`` 集合（跳过空值）；无匹配时为空集合。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            无（仅在该事务内执行一次只读查询）。
        """

        if not task_ids:
            return set()
        rows = session.execute(
            select(ConversationRunModel.checkpoint_thread_id).where(
                ConversationRunModel.task_id.in_(task_ids)
            )
        ).all()
        return {row[0] for row in rows if row[0]}

    def collect_checkpoint_threads_by_thread_ids(
        self, session: Session, thread_ids: set[str]
    ) -> set[str]:
        """返回主库中仍被任意 run 引用的 checkpoint thread id 集合。

        用于级联删除后判断哪些 thread 已成孤儿：传入删除前收集到的 thread 集合，
        返回其中仍被主库剩余 run 引用的子集，差集即为可 GC 的孤儿。

        参数:
            session: 处于事务中的 SQLAlchemy session（本方法只读、不提交）。
            thread_ids: 待判定的 checkpoint thread id 集合；为空时直接返回空集合。

        返回:
            仍被主库 run 引用的 thread id 集合；无匹配时为空集合。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            无（仅在该事务内执行一次只读查询）。
        """

        if not thread_ids:
            return set()
        rows = session.execute(
            select(ConversationRunModel.checkpoint_thread_id).where(
                ConversationRunModel.checkpoint_thread_id.in_(thread_ids)
            )
        ).all()
        return {row[0] for row in rows if row[0]}

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
