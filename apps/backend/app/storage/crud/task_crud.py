"""``tasks`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``tasks`` 单表的增删改查与 model↔record 转换。

职责边界：
- 负责：task 单表读写、``TaskModel``↔``TaskRecord`` 转换。
- 不负责：跨表级联（如删除 task 时清理 turn / run / trace，由 ``service/task/`` 编排）、
  业务规则校验、事务跨多表编排。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，因此必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

from sqlalchemy import select, update

from app.models import TaskRecord
from app.storage.model.task_model import TaskModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import from_text, to_text, utc_now


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
        task_id: str,
        workspace_id: str,
        agent_id: str,
        input_text: str,
        title: str,
        last_message_preview: str,
        latest_turn_id: str,
        status: str,
    ) -> TaskRecord:
        """新建一条 task 记录并落库。

        ``task_id`` 由调用方提供（通常为 UUID），``created_at`` / ``updated_at`` 由本方法以
        当前 UTC 时间统一填充。

        参数:
            task_id: 任务唯一标识（调用方保证全局唯一）。
            workspace_id: 所属工作区标识。
            agent_id: 执行该任务的 agent 标识。
            input_text: 任务的原始输入文本。
            title: 任务标题。
            last_message_preview: 最近一条消息的预览文本。
            latest_turn_id: 最新一轮对话的 turn 标识。
            status: 任务初始状态。

        返回:
            落库成功的 ``TaskRecord``（含填充好的创建 / 更新时间）。

        异常:
            sqlalchemy.exc.IntegrityError: 如果 task_id 冲突或违反约束。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``tasks`` 表插入一行。
        """
        now = utc_now()
        task = TaskRecord(
            task_id=task_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            input_text=input_text,
            title=title,
            last_message_preview=last_message_preview,
            latest_turn_id=latest_turn_id,
            status=status,
            created_at=now,
            updated_at=now,
        )
        with self._session_factory.begin() as session:
            session.add(
                TaskModel(
                    task_id=task.task_id,
                    workspace_id=task.workspace_id,
                    agent_id=task.agent_id,
                    input_text=task.input_text,
                    title=task.title,
                    last_message_preview=task.last_message_preview,
                    latest_turn_id=task.latest_turn_id,
                    status=task.status,
                    created_at=to_text(task.created_at),
                    updated_at=to_text(task.updated_at),
                )
            )
        return task

    def list_by_workspace(self, workspace_id: str) -> list[TaskRecord]:
        """列出某工作区下的全部 task，按更新时间倒序。

        参数:
            workspace_id: 工作区标识。

        返回:
            该工作区的 task 列表，按 ``updated_at`` 再 ``task_id`` 倒序排列；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(TaskModel)
                    .where(TaskModel.workspace_id == workspace_id)
                    .order_by(TaskModel.updated_at.desc(), TaskModel.task_id.desc())
                )
                .scalars()
                .all()
            )
        return [self._task_from_model(row) for row in rows]

    def get(self, task_id: str) -> TaskRecord:
        """按标识返回单个 task。

        参数:
            task_id: 任务标识。

        返回:
            匹配的 ``TaskRecord``。

        异常:
            KeyError: 如果指定 task 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        with self._session_factory() as session:
            row = session.get(TaskModel, task_id)
        if row is None:
            raise KeyError(task_id)
        return self._task_from_model(row)

    def update_status(self, task_id: str, status: str) -> TaskRecord:
        """更新 task 状态并刷新更新时间。

        先校验 task 存在（不存在则抛出），再更新状态与 ``updated_at``。

        参数:
            task_id: 任务标识。
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
                .where(TaskModel.task_id == task_id)
                .values(status=status, updated_at=to_text(utc_now()))
            )
        return self.get(task_id)

    def has_status(self, task_id: str, status: str) -> bool:
        """判断 task 当前状态是否等于给定值。

        参数:
            task_id: 任务标识。
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

    def update_latest_turn(
        self, task_id: str, latest_turn_id: str, last_message_preview: str
    ) -> None:
        """更新 task 的最新 turn 指针与消息预览。

        参数:
            task_id: 任务标识。
            latest_turn_id: 最新一轮对话的 turn 标识。
            last_message_preview: 最近一条消息的预览文本。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            更新 ``tasks`` 表中对应行的 latest_turn_id、last_message_preview 与 updated_at。
            注意：不校验 task 是否存在，task 不存在时静默无更新。
        """
        with self._session_factory.begin() as session:
            session.execute(
                update(TaskModel)
                .where(TaskModel.task_id == task_id)
                .values(
                    latest_turn_id=latest_turn_id,
                    last_message_preview=last_message_preview,
                    updated_at=to_text(utc_now()),
                )
            )

    def list_ids_by_workspace(self, workspace_id: str) -> list[str]:
        """仅返回某工作区下全部 task 的标识列表。

        相比 ``list_by_workspace``，本方法只查 ``task_id`` 一列，用于跨表级联删除等只需 id
        的场景，避免整行读取。

        参数:
            workspace_id: 工作区标识。

        返回:
            该工作区的 task_id 列表；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        from sqlalchemy import select

        with self._session_factory() as session:
            return [
                row[0]
                for row in session.execute(
                    select(TaskModel.task_id).where(TaskModel.workspace_id == workspace_id)
                ).all()
            ]

    def delete_by_ids(self, task_ids: list[str]) -> None:
        """按标识批量删除 task。

        参数:
            task_ids: 待删除的 task 标识列表。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``tasks`` 表删除匹配的行。仅删除 task 自身，不级联清理 turn / run / trace
            （级联由上层 service 编排）。
        """
        from sqlalchemy import delete

        with self._session_factory.begin() as session:
            session.execute(delete(TaskModel).where(TaskModel.task_id.in_(task_ids)))

    def _task_from_model(self, row: TaskModel) -> TaskRecord:
        """把 ``TaskModel`` ORM 行转换为业务 ``TaskRecord``。

        转换过程把库中存储的文本时间戳还原为 datetime。

        参数:
            row: 查询得到的 ``TaskModel`` 行。

        返回:
            对应的 ``TaskRecord``。

        异常:
            无。

        副作用:
            无。
        """
        return TaskRecord(
            task_id=row.task_id,
            workspace_id=row.workspace_id,
            agent_id=row.agent_id,
            input_text=row.input_text,
            title=row.title,
            last_message_preview=row.last_message_preview,
            latest_turn_id=row.latest_turn_id,
            status=row.status,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
        )
