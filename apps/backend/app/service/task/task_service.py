"""Task orchestration service.

单一职责：编排任务创建（仅建 task 容器，首轮次由调用方显式创建）与执行态派生。

职责边界：
- 负责：任务容器创建（不含首轮次）、从最新 turn 派生执行态、任务树原子级联删除
  （委托给 ``CascadeDeleter``）。
- 不负责：直接 SQL 操作（委托给 ``TaskCrud``/``ConversationRunCrud``/``WorkspaceCrud``/
  ``CascadeDeleter``）；不写执行态（执行态由 ``Turn`` 持有，本 service 仅派生展示）；
  不绑定 agent（agent 维度由 turn 与 delegation 记录承载）。
"""

from dataclasses import replace

from app.config.logging.logger import log
from app.models import ConversationRunRecord, TaskRecord
from app.service import depends as service_depends
from app.utils.datetime_utils import preview


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
        self._turn = service_depends.get_conversation_run_crud()
        self._workspace = service_depends.get_workspace_crud()
        self._cascade_deleter = service_depends.get_cascade_deleter()

    def create_task(
        self,
        input_text: str,
        workspace_id: int | None = None,
    ) -> TaskRecord:
        """创建任务容器记录（不含首轮次，首轮次由调用方显式调 ``create_run``）。

        任务文本 ``input_text`` 归属 turn 维度（首轮次创建时写入 ``turns.input_text``），
        任务本身只持久化由 ``input_text`` 派生的 ``title``。因 ``turns.task_id`` 外键指向
        ``tasks.id``，必须先有 task 才能创建首 turn；但 task 创建与首 turn 创建已解耦，
        本方法只建 task 容器，首 turn 由调用方（API/前端）随后显式创建。

        任务不再绑定 agent：agent 维度由 turn（首 turn 的 ``agent_id``）承载，
        ``create_task`` 不再接收也不校验 agent_id；子任务的 agent 由 delegation 记录承载。

        参数:
            input_text: 用户输入文本，用于派生任务标题（仅派生 title，原文不落 tasks 表）。
            workspace_id: 所属工作区标识。

        返回:
            已持久化的 ``TaskRecord``。

        异常:
            ValueError: 当 ``input_text``/``workspace_id`` 为空或全空白时抛出。
            sqlalchemy.exc.IntegrityError: 当 ``workspace_id`` 指向不存在的工作区
                （外键约束）时抛出。
            sqlalchemy.exc.SQLAlchemyError: 如果底层写入失败。

        副作用:
            向 ``tasks`` 表插入一行任务记录（不创建轮次）。
        """

        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("input_text must be a non-empty string")

        if not isinstance(workspace_id, int) or workspace_id <= 0:
            raise ValueError("workspace_id must be a positive integer")

        title = preview(input_text)
        # 先创建 task（turns.task_id 外键指向 tasks.id，必须先有 task 才能建 turn）。
        # 首 turn 由调用方随后显式创建，本方法不再耦合首 turn 逻辑。
        task = self._task.create(
            workspace_id=workspace_id,
            title=title,
        )

        return task

    def get_task(self, task_id: int) -> TaskRecord:
        """按标识取单个任务，并附带派生的执行态。

        参数:
            task_id: 任务标识。

        返回:
            对应的 ``TaskRecord``，其 ``execution_status`` 由最新轮次派生
            （见 ``task_display_status``）。

        异常:
            KeyError: 如果指定任务不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            无（仅读取）。
        """
        record = self._task.get(task_id)
        return replace(record, execution_status=self.task_display_status(task_id))

    def update_context_usage(self, task_id: int, used: int) -> TaskRecord:
        """持久化任务最近一次上下文窗口已用 token。

        供运行时在每次模型步产出上下文占用事件后调用，使「打开历史任务」时可回显
        该任务最近一次的真实占用。total 不落库，由 ``resolve_context_window``
        动态计算（见 ``get_task`` API）。

        参数:
            task_id: 任务标识。
            used: 最近一次上下文窗口已用 token 数。

        返回:
            更新后的 ``TaskRecord``。

        异常:
            KeyError: 如果指定 task 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            更新 ``tasks`` 表对应行的 context_usage_used 与 updated_at。
        """

        if used < 0:
            # 已用 token 不可能为负；调用方传入负数属异常数据，clamp 为 0 并告警，
            # 避免脏数据落库影响前端占比展示。
            log.warning(
                "context_usage_negative_clamped",
                extra={
                    "msg": "已用 token 为负，按 0 处理",
                    "data": {"task_id": task_id, "used": used},
                },
            )
            used = 0

        return self._task.update_context_usage(task_id, used)

    def task_display_status(self, task_id: int) -> str:
        """从最新轮次派生任务的执行态。

        映射规则：最新轮次为 ``running`` → ``"active"``；为
        ``completed``/``failed``/``cancelled`` 等终态 → 对应状态值；无任何轮次 → ``"empty"``。

        参数:
            task_id: 任务标识。

        返回:
            派生的执行态字符串。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            无（仅读取）。
        """
        turns: list[ConversationRunRecord] = self._turn.list_by_task(task_id)
        if not turns:
            return "empty"
        latest = turns[-1]
        if latest.status == "running":
            return "active"
        return latest.status

    def list_tasks_for_workspace(self, workspace_id: int) -> list[TaskRecord]:
        """列出某工作区下的用户任务（排除委派子任务）。

        委派子任务（``task_type='delegation'``）不进侧边栏对话列表，故本方法仅返回
        ``task_type='user'`` 的任务，按更新时间倒序。

        参数:
            workspace_id: 工作区标识。

        返回:
            该工作区下的用户任务记录列表（不含委派子任务）；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果底层查询失败。

        副作用:
            无（仅读取）。
        """
        return self._task.list_by_workspace(workspace_id)

    def list_child_tasks(self, parent_task_id: int) -> list[TaskRecord]:
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
        parent_task_id: int,
        parent_run_id: int,
        delegation_id: int,
        workspace_id: int,
    ) -> TaskRecord:
        """创建委派子任务（只建 task，不建 turn）。

        与用户任务不同，委派子任务不进侧边栏、无首 turn（turn 由委派执行器单独创建）、
        不参与 archived 生命周期交互。``task_type`` 固定为 ``"delegation"``，并通过
        ``parent_task_id`` / ``parent_run_id`` / ``delegation_id`` 关联父任务与委派记录。
        ``title`` 使用 ``input_text`` 的预览文本（子任务无侧边栏展示，但保留可读标题便于排查）。

        子任务不再绑定 agent：agent 关系由 ``DelegationRecord``（``child_agent_id`` /
        ``parent_agent_id``）承载，本方法不接收也不校验 agent_id，仅建 task 容器。

        参数:
            parent_task_id: 父任务标识。
            parent_run_id: 触发委派的父 turn 标识。
            delegation_id: 关联的委派记录标识（唯一索引兜底并发重入）。
            workspace_id: 所属工作区标识。
            title: 子任务标题（由委派输入文本预览得到，仅用于排查，不进侧边栏）。

        返回:
            已持久化的子任务 ``TaskRecord``（``task_type='delegation'``）。

        异常:
            ValueError: 如果任意必填字段为空或非法。
            sqlalchemy.exc.IntegrityError: 如果 ``delegation_id`` 重复（并发重入）或外键冲突。
            sqlalchemy.exc.SQLAlchemyError: 如果底层写入失败。

        副作用:
            向 ``tasks`` 表插入一行 delegation 类型的子任务记录（不建 turn）。
        """
        for field_name, value in (
            ("parent_task_id", parent_task_id),
            ("parent_run_id", parent_run_id),
            ("workspace_id", workspace_id),
        ):
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")

        return self._task.create(
            workspace_id=workspace_id,
            title=title,
            task_type="delegation",
            parent_task_id=parent_task_id,
            parent_run_id=parent_run_id,
            delegation_id=delegation_id,
        )

    def delete_task(self, task_id: int) -> None:
        """原子删除任务树并级联清理其下全部子产物。

        删除前先校验任务存在（不存在则抛 ``KeyError``），再通过
        ``CascadeDeleter.delete_task_tree`` 在单个 ``BEGIN IMMEDIATE`` 写锁事务内
        递归清理其下全部子任务（委派子任务）及各自轮次、消息轨迹、运行时事件、文件
        快照与委派记录，保证原子性（全删或全不删）与并发安全（删除期间无并发写插入
        孤儿数据）。删除是高风险操作，保留 start / complete 审计日志。

        参数:
            task_id: 待删除的任务标识。

        返回:
            无。

        异常:
            KeyError: 如果指定任务不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果级联删除失败。

        副作用:
            从 ``turn_messages`` / ``file_snapshots`` / ``conversation_runs`` / ``delegations``
            / ``tasks`` 表删除该任务树相关数据（旧 Runtime 事件体系已随对话事实重构
            一并删除，不再参与级联删除）。
        """

        self._task.get(task_id)  # 存在性守卫，不存在抛 KeyError
        log.info(
            "task_delete_start",
            extra={"msg": "task delete started", "data": {"task_id": task_id}},
        )
        deleted_count = self._cascade_deleter.delete_task_tree(task_id)
        log.info(
            "task_deleted",
            extra={
                "msg": "task deleted",
                "data": {"task_id": task_id, "deleted_tasks": deleted_count},
            },
        )
