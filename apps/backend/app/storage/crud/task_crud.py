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

    def create(
        self,
        workspace_id: int,
        title: str,
        status: str | None = None,
        task_type: str = "user",
        parent_task_id: int | None = None,
        parent_turn_id: int | None = None,
        delegation_id: int | None = None,
    ) -> TaskRecord:
        """新建一条 task 记录并落库。

        主键 ``id`` 由存储引擎自增分配，调用方不再提供业务标识。``created_at`` /
        ``updated_at`` 由本方法以当前 UTC 时间统一填充。``task_type`` 区分用户创建任务
        （``"user"``）与委派子任务（``"delegation"``）；委派子任务通过 ``parent_task_id`` /
        ``parent_turn_id`` / ``delegation_id``（均为整数 id）关联父任务与委派记录。

        任务不再绑定 agent：agent 维度由 turn（用户任务首 turn）与 delegation 记录
        （子任务）承载，本方法只持久化 task 容器自身字段。

        参数:
            workspace_id: 所属工作区标识（整数 id）。
            title: 任务标题。
            status: 任务初始状态，允许为 None，缺省时回退为 ``"pending"``。
            task_type: 任务类型，``"user"`` 或 ``"delegation"``，缺省为 ``"user"``。
            parent_task_id: 父任务标识（整数 id），委派子任务必填，用户任务为 None。
            parent_turn_id: 触发委派的父 turn 标识（整数 id），委派子任务必填，用户任务为 None。
            delegation_id: 关联的委派记录标识（整数 id），委派子任务必填，用户任务为 None。

        返回:
            落库成功的 ``TaskRecord``（含自增分配的 id 与填充的创建 / 更新时间）。

        异常:
            sqlalchemy.exc.IntegrityError: 如果违反外键约束或 delegation_id 重复。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``tasks`` 表插入一行。
        """
        now = utc_now()
        effective_status = status or "pending"
        with self._session_factory.begin() as session:
            model = TaskModel(
                workspace_id=workspace_id,
                title=title,
                status=effective_status,
                created_at=to_text(now),
                updated_at=to_text(now),
                task_type=task_type,
                parent_task_id=parent_task_id,
                parent_turn_id=parent_turn_id,
                delegation_id=delegation_id,
                context_usage_used=0,
            )
            session.add(model)
            session.flush()
            return TaskRecord.from_model(model)

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

    def update_status(self, task_id: int, status: str) -> TaskRecord:
        """更新 task 状态并刷新更新时间。

        先校验 task 存在（不存在则抛出），再更新状态与 ``updated_at``。

        参数:
            task_id: 任务标识（整数 id）。
            status: 新状态值。

        返回:
            更新后的 ``TaskRecord``。

        异常:
            KeyError: 如果指定 task 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            更新 ``tasks`` 表中对应行的 status 与 updated_at。
        """
        self.get(task_id)
        with self._session_factory.begin() as session:
            session.execute(
                update(TaskModel)
                .where(TaskModel.id == task_id)
                .values(status=status, updated_at=to_text(utc_now()))
            )
        return self.get(task_id)

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

    def has_status(self, task_id: int, status: str) -> bool:
        """判断 task 当前状态是否等于给定值。

        参数:
            task_id: 任务标识（整数 id）。
            status: 待比较的状态值。

        返回:
            当前状态等于 status 时返回 True，否则 False。

        异常:
            KeyError: 如果指定 task 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        return self.get(task_id).status == status

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
