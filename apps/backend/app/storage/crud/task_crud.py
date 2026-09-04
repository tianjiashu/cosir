"""``tasks`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``tasks`` 单表的增删改查与 model↔record 转换。

职责边界：
- 负责：task 单表读写、``TaskModel``↔``TaskRecord`` 转换。
- 不负责：跨表级联（如删除 task 时清理 turn / run / trace，由 ``service/task/`` 编排）、
  业务规则校验、事务跨多表编排。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，因此必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import TaskRecord
from app.storage.model.task_model import TaskModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


class TaskCrud:
    """``tasks`` 表的纯 CRUD。

    仅负责单表读写，不承担跨表编排与业务规则；所有方法通过共享的主库 session 工厂访问数据库。
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
            无（仅复用已初始化的主库 session 工厂，不创建 / 释放引擎）。
        """
        self._session_factory = main_session_factory()

    def ensure_task(self, session: Session, task_id: int) -> TaskRecord:
        task: TaskModel | None = session.get(TaskModel, task_id)
        if task is None:
            raise KeyError(task_id)
        return TaskRecord.from_model(task)

    def create(
        self,
        workspace_id: int,
        title: str,
        task_type: str = "user",
        parent_task_id: int | None = None,
        parent_run_id: int | None = None,
        delegation_id: int | None = None,
        creation_command_id: str | None = None,
        session: Session | None = None,
    ) -> TaskRecord:
        """新建一条 task 记录并落库。

        主键 ``id`` 由存储引擎自增分配，调用方不再提供业务标识。``created_at`` /
        ``updated_at`` 由本方法以当前 UTC 时间统一填充。``task_type`` 区分用户创建任务
        （``"user"``）与委派子任务（``"delegation"``）；委派子任务通过 ``parent_task_id`` /
        ``parent_run_id`` / ``delegation_id``（均为整数 id）关联父任务与委派记录。

        任务不再绑定 agent：agent 维度由 turn（用户任务首 turn）与 delegation 记录
        （子任务）承载，本方法只持久化 task 容器自身字段。

        参数:
            workspace_id: 所属工作区标识（整数 id）。
            title: 任务标题。
            task_type: 任务类型，``"user"`` 或 ``"delegation"``，缺省为 ``"user"``。
            parent_task_id: 父任务标识（整数 id），委派子任务必填，用户任务为 None。
            parent_run_id: 触发委派的父 turn 标识（整数 id），委派子任务必填，用户任务为 None。
            delegation_id: 关联的委派记录标识（整数 id），委派子任务必填，用户任务为 None。
            creation_command_id: 触发建任务的命令标识（整数 id），缺省为 None。
            session: 可选的外部 SQLAlchemy 会话。传入时在本方法内复用该事务（调用方负责提交）；
                为 None 时由本方法自开事务并自动提交。

        返回:
            落库成功的 ``TaskRecord``（含自增分配的 id 与填充的创建 / 更新时间）。

        异常:
            sqlalchemy.exc.IntegrityError: 如果违反外键约束或 delegation_id 重复。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``tasks`` 表插入一行；当 ``session`` 为 None 时由本方法提交事务。
        """
        if session is None:
            with self._session_factory.begin() as session:
                return self._build_and_flush(
                    session,
                    workspace_id,
                    title,
                    task_type,
                    parent_task_id,
                    parent_run_id,
                    delegation_id,
                    creation_command_id,
                )
        return self._build_and_flush(
            session,
            workspace_id,
            title,
            task_type,
            parent_task_id,
            parent_run_id,
            delegation_id,
            creation_command_id,
        )

    def _build_and_flush(
        self,
        session: Session,
        workspace_id: int,
        title: str,
        task_type: str,
        parent_task_id: int | None,
        parent_run_id: int | None,
        delegation_id: int | None,
        creation_command_id: str | None,
    ) -> TaskRecord:
        """在给定会话中构造并 flush 一条 task 记录。

        参数:
            session: 目标 SQLAlchemy 会话（已开启的事务）。
            workspace_id: 所属工作区标识。
            title: 任务标题。
            task_type: 任务类型。
            parent_task_id: 委派父任务标识。
            parent_run_id: 委派父轮次标识。
            delegation_id: 委派记录标识。
            creation_command_id: 触发建任务的命令标识。

        返回:
            已 flush 的 ``TaskRecord``。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 写入失败。

        副作用:
            在当前事务中插入一行任务；事务提交由调用方或 ``create`` 决定。
        """
        model = TaskModel(
            workspace_id=workspace_id,
            title=title,
            task_type=task_type,
            parent_task_id=parent_task_id,
            parent_run_id=parent_run_id,
            delegation_id=delegation_id,
            creation_command_id=creation_command_id,
            context_usage_used=0,
        )
        new_model: TaskModel | None = session.add(model)
        if new_model is None:
            raise RuntimeError("Failed to add task model to session")
        session.flush()
        return TaskRecord.from_model(new_model)

    def list_by_workspace(self, workspace_id: int) -> list[TaskRecord]:
        """列出某工作区下的用户任务（排除委派子任务），按更新时间倒序。

        委派子任务（``task_type='delegation'``）不出现在侧边栏对话列表中，因此本方法仅返回
        ``task_type='user'`` 的任务。

        参数:
            workspace_id: 工作区标识（整数 id）。

        返回:
            该工作区的用户任务列表，按 ``updated_at`` 再 ``id`` 倒序排列；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(TaskModel)
                    .where(
                        TaskModel.workspace_id == workspace_id,
                        TaskModel.task_type == "user",
                    )
                    .order_by(TaskModel.updated_at.desc(), TaskModel.id.desc())
                )
                .scalars()
                .all()
            )
        return [TaskRecord.from_model(row) for row in rows]

    def list_by_parent_task(self, parent_task_id: int) -> list[TaskRecord]:
        """展开某父任务下的全部子任务树（当前仅一层，对应 1 父 task ↔ N 子 task）。

        参数:
            parent_task_id: 父任务标识（整数 id）。

        返回:
            该父任务的直接子任务列表（``task_type='delegation'`` 且 ``parent_task_id`` 匹配）；
            无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(TaskModel)
                    .where(TaskModel.parent_task_id == parent_task_id)
                    .order_by(TaskModel.created_at.asc(), TaskModel.id.asc())
                )
                .scalars()
                .all()
            )
        return [TaskRecord.from_model(row) for row in rows]

    def get(self, task_id: int) -> TaskRecord:
        """按标识返回单个 task。

        参数:
            task_id: 任务标识（整数 id）。

        返回:
            匹配的 ``TaskRecord``。

        异常:
            KeyError: 如果指定 task 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        with self._session_factory() as session:
            row: TaskModel | None = session.get(TaskModel, task_id)
        if row is None:
            raise KeyError(task_id)
        return TaskRecord.from_model(row)

    def update_context_usage(self, task_id: int, used: int) -> TaskRecord:
        """更新 task 最近一次上下文窗口已用 token 并刷新更新时间。

        先校验 task 存在（不存在则抛出），再更新 ``context_usage_used`` 与 ``updated_at``。
        供运行时在每次模型步产出上下文占用事件后持久化，使「打开历史任务」时可回显
        该任务最近一次的真实占用（total 不落库，由 ``resolve_context_window`` 动态计算）。

        参数:
            task_id: 任务标识（整数 id）。
            used: 最近一次上下文窗口已用 token 数。

        返回:
            更新后的 ``TaskRecord``。

        异常:
            KeyError: 如果指定 task 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            更新 ``tasks`` 表中对应行的 context_usage_used 与 updated_at。
        """
        self.get(task_id)
        with self._session_factory.begin() as session:
            session.execute(
                update(TaskModel)
                .where(TaskModel.id == task_id)
                .values(context_usage_used=used, updated_at=to_text(utc_now()))
            )
        return self.get(task_id)

    def list_ids_by_workspace(self, workspace_id: int) -> list[int]:
        """仅返回某工作区下全部 task 的整数 id 列表。

        相比 ``list_by_workspace``，本方法只查 ``id`` 一列，用于跨表级联删除等只需 id
        的场景，避免整行读取。

        参数:
            workspace_id: 工作区标识（整数 id）。

        返回:
            该工作区的 task id 列表；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        with self._session_factory() as session:
            return [
                row[0]
                for row in session.execute(
                    select(TaskModel.id).where(TaskModel.workspace_id == workspace_id)
                ).all()
            ]

    def delete_by_ids(self, task_ids: list[int]) -> None:
        """按标识批量删除 task。

        参数:
            task_ids: 待删除的 task 整数 id 列表。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``tasks`` 表删除匹配的行。仅删除 task 自身，不级联清理 turn / run / trace
            （级联由上层 service 编排）；task_ids 为空或对应行不存在时静默无操作。
        """

        if not task_ids:
            return
        with self._session_factory.begin() as session:
            session.execute(delete(TaskModel).where(TaskModel.id.in_(task_ids)))
