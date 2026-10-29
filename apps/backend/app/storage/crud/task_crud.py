"""``tasks`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``tasks`` 单表的增删改查与 model↔record 转换。

职责边界：
- 负责：task 单表读写、``TaskModel``↔``TaskRecord`` 转换。
- 不负责：跨表级联（如删除 task 时清理 turn / run / trace，由 ``service/task/`` 编排）、
  业务规则校验、事务跨多表编排。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，因此必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

from typing import Any

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
            creation_command_id: str | None = None,
            session: Session | None = None,
            extra: dict[str, Any] | None = None,
            tool_schemas: list[dict[str, Any]] | None = None,
    ) -> TaskRecord:
        """新建一条 task 记录并落库。

        主键 ``id`` 由存储引擎自增分配，``created_at`` / ``updated_at`` 由本方法以当前 UTC 时间
        统一填充。子 Task（委派、Team 节点）只经 ``parent_task_id`` 挂到父 Task，不记录发起它的
        父 Run：子 Task 会被父 Agent 在后续多个 Run 中复用，创建时的 Run 无法代表归属。Agent
        身份不落在 Task 上，随入队输入在建 Run 时固化到 Run。

        参数:
            workspace_id: 所属工作区标识（整数 id）。
            title: 任务标题。
            task_type: 任务类型，如 ``"user"``、``"fork"``、``"delegate_task"``、
                ``"agent_team_node"``，缺省为 ``"user"``。
            parent_task_id: 父任务标识（整数 id），子 Task 必填，用户任务为 None。
            creation_command_id: 前端创建命令标识，用于首条命令未被接受时回收 provisional Task。
            session: 可选的外部 SQLAlchemy 会话。传入时在本方法内复用该事务（调用方负责提交）；
                为 None 时由本方法自开事务并自动提交。
            extra: Task 扩展 JSON（如 fork 来源、Team 节点快照）。
            tool_schemas: 创建时固化的模型工具 schema。

        返回:
            落库成功的 ``TaskRecord``（含自增分配的 id 与填充的创建 / 更新时间）。

        异常:
            sqlalchemy.exc.IntegrityError: 如果违反外键约束或同一工作区内 creation_command_id 重复。
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
                    creation_command_id,
                    extra,
                    tool_schemas,
                )
        return self._build_and_flush(
            session,
            workspace_id,
            title,
            task_type,
            parent_task_id,
            creation_command_id,
            extra,
            tool_schemas,
        )

    def _build_and_flush(
            self,
            session: Session,
            workspace_id: int,
            title: str,
            task_type: str,
            parent_task_id: int | None,
            creation_command_id: str | None,
            extra: dict[str, Any] | None,
            tool_schemas: list[dict[str, Any]] | None = None,
    ) -> TaskRecord:
        """在给定会话中构造并 flush 一条 task 记录。

        参数:
            session: 目标 SQLAlchemy 会话（已开启的事务）。
            workspace_id: 所属工作区标识。
            title: 任务标题。
            task_type: 任务类型。
            parent_task_id: 委派父任务标识。
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
            creation_command_id=creation_command_id,
            extra=extra,
            tool_schemas=tool_schemas or [],
            current_run_id=None,
        )
        session.add(model)
        session.flush()
        return TaskRecord.from_model(model)

    def next_fork_title(self, workspace_id: int, base_title: str, session: Session) -> str:
        """在外部事务中生成当前工作区内可读的 fork 标题。"""

        titles = set(
            session.scalars(
                select(TaskModel.title).where(TaskModel.workspace_id == workspace_id)
            ).all()
        )
        suffix = 1
        candidate = f"{base_title} {suffix}"
        while candidate in titles:
            suffix += 1
            candidate = f"{base_title} {suffix}"
        return candidate

    def list_fork_sources(self, workspace_id: int) -> dict[int, int]:
        """返回工作区内 fork 与其直接来源的对应（fork id → 来源 id）；没有来源的 fork 不在其中。"""

        with self._session_factory() as session:
            rows = session.execute(
                select(TaskModel.id, TaskModel.extra).where(
                    TaskModel.workspace_id == workspace_id, TaskModel.task_type == "fork"
                )
            ).all()
        sources: dict[int, int] = {}
        for row_id, extra in rows:
            fork = (extra or {}).get("fork")
            source = fork.get("source_task_id") if isinstance(fork, dict) else None
            if isinstance(source, int):
                sources[row_id] = source
        return sources

    def reparent_forks(
        self,
        workspace_id: int,
        source_ids: frozenset[int],
        new_source_id: int | None,
        session: Session,
    ) -> int:
        """在外部事务中把来源在 ``source_ids`` 里的 fork 改挂到 ``new_source_id``，返回改动行数。

        fork 的来源记在 ``extra.fork.source_task_id``（不是 ``parent_task_id``，后者只承载所有权）。
        ``new_source_id`` 为 ``None`` 时去掉来源，侧栏把它当作根。``source_ids`` 自身不改：它们随后
        被删除。刻意不刷新 ``updated_at``：侧栏按它排序，重挂不是对话活动。
        """

        rows = session.execute(
            select(TaskModel.id, TaskModel.extra).where(
                TaskModel.workspace_id == workspace_id,
                TaskModel.task_type == "fork",
                TaskModel.id.not_in(source_ids),
            )
        ).all()
        moved = 0
        for row_id, extra in rows:
            fork = (extra or {}).get("fork")
            if not isinstance(fork, dict) or fork.get("source_task_id") not in source_ids:
                continue
            fork = {key: value for key, value in fork.items() if key != "source_task_id"}
            if new_source_id is not None:
                fork["source_task_id"] = new_source_id
            session.execute(
                update(TaskModel)
                .where(TaskModel.id == row_id)
                .values(extra={**extra, "fork": fork}, updated_at=TaskModel.updated_at)
            )
            moved += 1
        return moved

    def list_by_parent_task(self, parent_task_id: int) -> list[TaskRecord]:
        """返回某父任务直接委派的子 Agent Task（``task_type='delegate_task'``），供展示使用。

        只看一层且排除 Agent Team 节点 Task；需要整棵任务树（如删除）时用
        ``list_child_ids``，不要复用本方法，否则会漏掉其他类型的子 Task。
        """
        stmt = (
            select(TaskModel)
            .where(
                TaskModel.parent_task_id == parent_task_id,
                TaskModel.task_type == "delegate_task",
            )
            .order_by(TaskModel.created_at.asc(), TaskModel.id.asc())
        )
        with self._session_factory() as session:
            rows = session.scalars(stmt).all()
        return [TaskRecord.from_model(row) for row in rows]

    def list_child_ids(self, parent_task_id: int, session: Session) -> list[int]:
        """在调用方事务内返回挂在某父任务下的全部直接子 Task id，不区分 ``task_type``。

        任务树遍历的唯一入口：``tasks.parent_task_id`` 自引用外键对委派子 Task 与 Team 节点
        Task 一视同仁，按类型过滤会让删除漏掉子行而触发外键失败。
        """
        return list(
            session.scalars(select(TaskModel.id).where(TaskModel.parent_task_id == parent_task_id))
        )

    def list_by_parent_task_ids(self, parent_task_ids: list[int]) -> dict[int, list[TaskRecord]]:
        """批量读取一组任务直接委派的子 Agent Task。"""

        if not parent_task_ids:
            return {}
        with self._session_factory() as session:
            rows = session.scalars(
                select(TaskModel)
                .where(
                    TaskModel.parent_task_id.in_(parent_task_ids),
                    TaskModel.task_type == "delegate_task",
                )
                .order_by(TaskModel.created_at.asc(), TaskModel.id.asc())
            ).all()
        grouped = {task_id: [] for task_id in parent_task_ids}
        for row in rows:
            grouped[row.parent_task_id].append(TaskRecord.from_model(row))
        return grouped

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

    def set_current_run_id(
            self,
            task_id: int,
            run_id: int,
            session: Session | None = None,
    ) -> TaskRecord:
        """在同一事务中更新 Task 当前 Run 引用。"""

        values: dict[str, object] = {
            "current_run_id": run_id,
            "updated_at": to_text(utc_now()),
        }
        statement = (
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(**values)
        )
        if session is not None:
            result = session.execute(statement)
            if not result.rowcount:
                raise KeyError(task_id)
            session.flush()
            row: TaskModel | None = session.get(TaskModel, task_id)
            assert row is not None
            return TaskRecord.from_model(row)
        with self._session_factory.begin() as owned_session:
            result = owned_session.execute(statement)
            if not result.rowcount:
                raise KeyError(task_id)
        return self.get(task_id)

    def update_title(self, task_id: int, title: str) -> None:
        """改写 Task 标题；刻意不刷新 ``updated_at``：侧栏按它排序，改名不该让对话换位置。

        异常:
            KeyError: Task 不存在（含已被删除）。
        """

        with self._session_factory.begin() as session:
            # 显式把 updated_at 写回原值：基类的 onupdate 对 Core update 同样生效。
            result = session.execute(
                update(TaskModel)
                .where(TaskModel.id == task_id)
                .values(title=title, updated_at=TaskModel.updated_at)
            )
            if not result.rowcount:
                raise KeyError(task_id)

    def list_ids_by_workspace(self, workspace_id: int, session: Session | None = None) -> list[int]:
        """仅返回某工作区下全部 task 的整数 id 列表。

        本方法只查 ``id`` 一列，用于跨表级联删除等只需 id
        的场景，避免整行读取。

        参数:
            workspace_id: 工作区标识（整数 id）。
            session: 可选的调用方事务 session；传入时复用当前事务，不自行提交。

        返回:
            该工作区的 task id 列表；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """
        stmt = select(TaskModel.id).where(TaskModel.workspace_id == workspace_id)
        if session is not None:
            return list(session.scalars(stmt).all())
        with self._session_factory() as owned_session:
            return list(owned_session.scalars(stmt).all())

    def list_ids_with_current_run_status(
            self, status: str, session: Session | None = None
    ) -> list[int]:
        """返回当前 Run 处于 ``status`` 的 task id；启动时找出挂起中、需要 actor 等唤醒的 Task。"""
        from app.storage.model.conversation_run_model import ConversationRunModel

        stmt = (
            select(TaskModel.id)
            .join(ConversationRunModel, ConversationRunModel.id == TaskModel.current_run_id)
            .where(ConversationRunModel.status == status)
        )
        if session is not None:
            return list(session.scalars(stmt).all())
        with self._session_factory() as owned_session:
            return list(owned_session.scalars(stmt).all())

    def list_root_ids_by_workspace(
            self, workspace_id: int, session: Session | None = None
    ) -> list[int]:
        """返回某工作区下「主任务」（``parent_task_id`` 为 NULL）的整数 id 列表。

        主任务对应于用户任务 / fork 任务等根节点，其下通过 ``parent_task_id`` 自引用挂接
        委派子任务。级联删除工作区时只需删除这些根任务，``TaskService.delete_task`` 会沿
        ``parent_task_id`` 递归清理整棵子树（含委派子任务），从而覆盖工作区内全部 task。

        参数:
            workspace_id: 工作区标识（整数 id）。
            session: 可选的调用方事务 session；传入时复用当前事务，不自行提交。

        返回:
            该工作区下根任务（``parent_task_id IS NULL``）的 id 列表；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        stmt = select(TaskModel.id).where(
            TaskModel.workspace_id == workspace_id,
            TaskModel.parent_task_id.is_(None),
        )
        if session is not None:
            return list(session.scalars(stmt).all())
        with self._session_factory() as owned_session:
            return list(owned_session.scalars(stmt).all())

    def delete_by_ids(self, task_ids: list[int], session: Session | None = None) -> None:
        """按标识批量删除 task。

        参数:
            task_ids: 待删除的 task 整数 id 列表。
            session: 可选外部事务 session；传入时复用该事务不自行提交，为 None 时
                自开事务并自动提交。

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
        if session is not None:
            session.execute(delete(TaskModel).where(TaskModel.id.in_(task_ids)))
            return
        with self._session_factory.begin() as session:
            session.execute(delete(TaskModel).where(TaskModel.id.in_(task_ids)))
