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
from app.models import TaskRecord
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

    def create_task(
        self,
        input_text: str,
        status: str,
        agent_id: str = "developer",
        workspace_id: str | None = None,
    ) -> TaskRecord:
        """Create a task and its first turn (atomic).

        ``status`` 表示用户驱动的**生命周期**（open/archived），与执行态分离；
        首个轮次固定为 ``pending`` 执行态。因 ``turns.task_id`` 外键指向 ``tasks.task_id``，
        必须先创建 task（``latest_turn_id`` 暂置空），再创建首 turn 并用真实
        ``turn_id`` 回写 task 的最新轮次指针，保证 ``latest_turn_id`` 与实际一致。
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
        self._task.create(
            task_id=task_id,
            workspace_id=resolved_workspace_id,
            agent_id=agent_id,
            input_text=input_text,
            title=title,
            last_message_preview=title,
            latest_turn_id=None,
            status=status,
        )
        # 再创建首 turn，并用真实 turn_id 回写 task 的最新轮次指针。
        first_turn = self._turn.create(
            task_id=task_id,
            input_text=input_text,
            status="pending",
            agent_id=agent_id,
        )
        self._task.update_latest_turn(task_id, first_turn.turn_id, title)
        # 重新取回带最新 latest_turn_id 的 task 记录返回给调用方。
        return self._task.get(task_id)

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

    def delete_task(self, task_id: str) -> None:
        """删除单个任务并级联清理其下轮次、消息轨迹与运行时事件。

        删除前先校验任务存在（不存在则抛 ``KeyError``），再按
        ``runtime_events -> turn_messages -> turns -> task`` 顺序清理，
        避免外键 / 孤儿数据。
        删除是高风险操作，保留 start / complete 审计日志。

        参数:
            task_id: 待删除的任务标识。

        返回:
            无。

        异常:
            KeyError: 如果指定任务不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果级联删除失败。

        副作用:
            从 ``runtime_events`` / ``turn_messages`` / ``turns`` / ``tasks``
            表删除该任务相关数据。
        """

        self._task.get(task_id)  # 存在性守卫，不存在抛 KeyError
        log.info(
            "task_delete_start",
            extra={"msg": "task delete started", "data": {"task_id": task_id}},
        )
        turn_ids = self._turn.list_ids_by_task_ids([task_id])
        if turn_ids:
            self._runtime_event.delete_by_turn_ids(turn_ids)
            self._turn_message.delete_by_turn_ids(turn_ids)
            self._turn.delete_by_ids(turn_ids)
        self._task.delete_by_ids([task_id])
        log.info(
            "task_deleted",
            extra={"msg": "task deleted", "data": {"task_id": task_id, "turn_ids": turn_ids}},
        )
