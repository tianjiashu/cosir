"""Task orchestration service.

单一职责：编排任务创建（含初始轮次创建）、生命周期管理（open/archived）与执行态派生。

职责边界：
- 负责：任务创建（同时创建首个轮次）、用户驱动的生命周期状态、从最新 turn 派生执行态。
- 不负责：直接 SQL 操作（委托给 ``TaskCrud``/``TurnCrud``/``WorkspaceCrud``）；
  不写执行态（执行态由 ``Turn`` 持有，本 service 仅派生展示）。
"""

from dataclasses import replace
from uuid import uuid4

from app.config.configuration import build_agent_registry, get_agent_registry
from app.config.logging.logger import log
from app.models import TaskRecord, TurnRecord
from app.service import depends as service_depends
from app.utils.datetime_utils import preview


def _registered_agent_ids() -> set[str]:
    """返回当前已注册的 agent_id 集合。

    优先复用进程级 registry 单例（由应用启动经 ``set_agent_registry`` 注入，
    供 API 层依赖注入与校验共享同一份）；单例未注入（如单元 / 脚本场景）时，
    回退到 ``build_agent_registry`` 临时构建一份只读目录用于校验，避免模块级
    缓存导致与运行态不一致。

    参数:
        无。

    返回:
        已注册 agent_id 的集合。

    异常:
        无。

    副作用:
        单例未注入时临时构造一个 registry 实例（仅用于本次校验，不写入单例）。
    """

    try:
        return get_agent_registry().list_agent_ids()
    except RuntimeError:
        return build_agent_registry().list_agent_ids()


class TaskService:
    """Orchestrate task creation, lifecycle management, and execution-status derivation."""

    def __init__(self) -> None:
        """初始化任务 service。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 storage 尚未初始化。

        副作用:
            从 service 依赖入口取得 CRUD 单例并保存引用。
        """

        self._task = service_depends.get_task_crud()
        self._turn = service_depends.get_turn_crud()
        self._workspace = service_depends.get_workspace_crud()
        self._runtime_event = service_depends.get_runtime_event_crud()
        self._turn_message = service_depends.get_turn_message_crud()
        self._delegation = service_depends.get_delegation_crud()

    def create_task(
        self,
        input_text: str,
        status: str,
        agent_id: str = "developer",
        workspace_id: str | None = None,
    ) -> TaskRecord:
        """创建任务记录（不含首轮次，首轮次在执行时由 turn 维度创建）。

        ``status`` 表示用户驱动的**生命周期**（open/archived），与执行态分离。任务文本
        ``input_text`` 归属 turn 维度（首轮次创建时写入 ``turns.input_text``），任务本身只
        持久化由 ``input_text`` 派生的 ``title``。因 ``turns.task_id`` 外键指向 ``tasks.task_id``，
        必须先有 task 才能在执行阶段创建首 turn。
        """

        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("input_text must be a non-empty string")

        if workspace_id is None or not isinstance(workspace_id, str) or not workspace_id.strip():
            raise ValueError("workspace_id must be a non-empty string")

        if agent_id is None or not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("agent_id must be a non-empty string")

        if agent_id not in _registered_agent_ids():
            raise ValueError(f"agent_id {agent_id} is not registered")

        resolved_workspace_id = workspace_id
        title = preview(input_text)
        task_id = str(uuid4())
        # 先创建 task（turns.task_id 外键指向 tasks.task_id，必须先有 task 才能建 turn）。
        task = self._task.create(
            task_id=task_id,
            workspace_id=resolved_workspace_id,
            agent_id=agent_id,
            title=title,
            status=status,
        )

        return task

    def get_task(self, task_id: str) -> TaskRecord:
        """Return the task with its derived ``execution_status`` attached."""

        record = self._task.get(task_id)
        return replace(record, execution_status=self.task_display_status(task_id))

    def update_status(self, task_id: str, status: str) -> TaskRecord:
        """Update the task lifecycle status (open/archived)."""

        return self._task.update_status(task_id, status)

    def set_lifecycle_status(self, task_id: str, status: str) -> TaskRecord:
        """Set the user-driven lifecycle status (open/archived) only.

        参数:
            task_id: 任务标识。
            status: ``"open"`` 或 ``"archived"``。

        返回:
            更新后的任务记录。
        """

        if status not in ("open", "archived"):
            raise ValueError("lifecycle status must be 'open' or 'archived'")
        return self._task.update_status(task_id, status)

    def task_display_status(self, task_id: str) -> str:
        """Derive the execution status from the latest turn.

        running -> "active"；completed/failed/cancelled -> 对应；无 turn -> "empty"。
        """

        turns = self._turn.list_by_task(task_id)
        if not turns:
            return "empty"
        latest = turns[-1]
        if latest.status == "running":
            return "active"
        return latest.status

    def has_status(self, task_id: str, status: str) -> bool:
        return self._task.has_status(task_id, status)

    def list_tasks_for_workspace(self, workspace_id: str) -> list[TaskRecord]:
        return self._task.list_by_workspace(workspace_id)

    def list_child_tasks(self, parent_task_id: str) -> list[TaskRecord]:
        """列出某父任务下的全部子任务（委派子任务）。

        参数:
            parent_task_id: 父任务标识。

        返回:
            该父任务的直接子任务列表（``task_type='delegation'``）；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            无（仅读取）。
        """

        return self._task.list_by_parent_task(parent_task_id)

    def create_child_task(
        self,
        *,
        title: str,
        parent_task_id: str,
        parent_turn_id: str,
        delegation_id: str,
        workspace_id: str,
        agent_id: str,
    ) -> TaskRecord:
        """创建委派子任务（只建 task，不建 turn）。

        与用户任务不同，委派子任务不进侧边栏、无首 turn（turn 由委派执行器单独创建）、
        不参与 archived 生命周期交互。``task_type`` 固定为 ``"delegation"``，并通过
        ``parent_task_id`` / ``parent_turn_id`` / ``delegation_id`` 关联父任务与委派记录。
        ``title`` 使用 ``input_text`` 的预览文本（子任务无侧边栏展示，但保留可读标题便于排查）。

        参数:
            parent_task_id: 父任务标识。
            parent_turn_id: 触发委派的父 turn 标识。
            delegation_id: 关联的委派记录标识（唯一索引兜底并发重入）。
            workspace_id: 所属工作区标识。
            agent_id: 执行该子任务的子 Agent 标识（须已注册）。
            title: 子任务标题（由委派输入文本预览得到，仅用于排查，不进侧边栏）。

        返回:
            已持久化的子任务 ``TaskRecord``（``task_type='delegation'``，``status='pending'``）。

        异常:
            ValueError: 如果 ``agent_id`` 未注册或任意必填字段为空。
            sqlalchemy.exc.IntegrityError: 如果 ``delegation_id`` 重复（并发重入）或外键冲突。
            sqlalchemy.exc.SQLAlchemyError: 如果底层写入失败。

        副作用:
            向 ``tasks`` 表插入一行 delegation 类型的子任务记录（不建 turn）。
        """
        for field_name, value in (
            ("parent_task_id", parent_task_id),
            ("parent_turn_id", parent_turn_id),
            ("delegation_id", delegation_id),
            ("workspace_id", workspace_id),
            ("agent_id", agent_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if agent_id not in _registered_agent_ids():
            raise ValueError(f"agent_id {agent_id} is not registered")

        task_id = str(uuid4())
        return self._task.create(
            task_id=task_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            title=title,
            status="pending",
            task_type="delegation",
            parent_task_id=parent_task_id,
            parent_turn_id=parent_turn_id,
            delegation_id=delegation_id,
        )

    def create_task_with_initial_turn(
        self,
        workspace_id: str,
        agent_id: str,
        input_text: str,
        status: str = "open",
    ) -> tuple[TaskRecord, TurnRecord]:
        """创建任务记录并同时创建其首个 pending 轮次，返回两者。

        这是「新建工作区即创建任务并进入首轮次」场景的单一编排入口：任务层
        只持有标题等轻量元数据（不含用户输入文本，用户输入文本归属轮次维度），
        首个轮次立即以 ``pending`` 状态创建，等待前端经
        ``POST /turns/{turn_id}/stream`` 认领并运行。

        参数:
            workspace_id: 所属工作区标识。
            agent_id: 执行该任务的 Agent 标识（须已注册）。
            input_text: 首个轮次的用户输入文本，同时用于派生任务标题。
            status: 任务初始状态，默认 ``"open"``。

        返回:
            ``(task_record, turn_record)`` 二元组，分别对应刚创建的顶层任务与其首个轮次。

        异常:
            ValueError: 如果 ``input_text`` 为空或全空白，或 ``agent_id`` 未注册。
            sqlalchemy.exc.IntegrityError: 如果 ``workspace_id`` 指向不存在的工作区
                （外键约束兜底，由底层 ``create_task`` 触发）。
            sqlalchemy.exc.SQLAlchemyError: 如果底层写入失败（任务或首轮次创建任一失败即抛出）。

        副作用:
            向 ``tasks`` 表插入一行顶层任务记录；向 ``turns`` 表插入一行 pending 首轮次记录
            （归属 ``task.task_id``，状态 ``pending``）。若 task 创建成功后首轮次写入失败，
            已提交的 task 会被显式补偿删除，避免残留孤儿任务。
        """
        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("input_text must be a non-empty string")

        task = self.create_task(
            workspace_id=workspace_id,
            agent_id=agent_id,
            input_text=input_text,
            status=status,
        )
        # 补偿式原子性：底层 task / turn 各自独立提交，若首轮次写入失败，已提交的 task
        # 无自动回滚，此处显式删除刚创建的 task，避免残留孤儿任务。极端情况下补偿删除
        # 自身失败时会保留 task 并向上抛出原始异常（由调用方错误日志捕获）。
        try:
            turn = self._turn.create(task.task_id, input_text, "pending")
        except Exception:
            self._task.delete_by_ids([task.task_id])
            raise
        return task, turn

    def delete_task(self, task_id: str) -> None:
        """递归删除任务并级联清理其下轮次、消息轨迹、运行时事件、子任务与委派记录。

        删除前先校验任务存在（不存在则抛 ``KeyError``），再递归删除其下全部子任务
        （委派子任务），然后按 ``runtime_events -> turn_messages -> turns -> delegations -> task``
        顺序清理自身数据，避免外键 / 孤儿数据。委派子 Agent 产生的 ``delegations`` 行以
        ``task_id`` 关联，若不复则删除后成为无法追溯的孤儿记录，因此须在此一并清理。
        删除是高风险操作，保留 start / complete 审计日志。

        参数:
            task_id: 待删除的任务标识。

        返回:
            无。

        异常:
            KeyError: 如果指定任务不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果级联删除失败。

        副作用:
            从 ``runtime_events`` / ``turn_messages`` / ``turns`` / ``delegations`` /
            ``tasks`` 表删除该任务及其子任务相关数据。
        """

        self._task.get(task_id)  # 存在性守卫，不存在抛 KeyError
        log.info(
            "task_delete_start",
            extra={"msg": "task delete started", "data": {"task_id": task_id}},
        )
        # 递归清理子任务（委派子任务），避免孤儿数据。
        child_tasks = self._task.list_by_parent_task(task_id)
        for child in child_tasks:
            self.delete_task(child.task_id)
        turn_ids = self._turn.list_ids_by_task_ids([task_id])
        if turn_ids:
            self._runtime_event.delete_by_turn_ids(turn_ids)
            self._turn_message.delete_by_turn_ids(turn_ids)
            self._turn.delete_by_ids(turn_ids)
        deleted_delegations = self._delegation.delete_by_task_ids([task_id])
        self._task.delete_by_ids([task_id])
        log.info(
            "task_deleted",
            extra={
                "msg": "task deleted",
                "data": {
                    "task_id": task_id,
                    "turn_ids": turn_ids,
                    "deleted_delegations": deleted_delegations,
                },
            },
        )
