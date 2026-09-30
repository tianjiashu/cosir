from pydantic import BaseModel

from app.models.task_record import TaskRecord
from app.utils.datetime_utils import to_text


class TaskResponse(BaseModel):
    """校验并序列化任务状态响应（与 ``TaskRecord.to_dict()`` 对齐）。

    任务不再绑定 agent：本响应不含 agent_id，agent 维度由 turn 维度承载。

    参数:
        task_id: 任务标识。
        workspace_id: 所属工作区标识。
        title: 任务标题。
        execution_status: 派生执行状态，可能为 None。
        task_type: 任务类型，``"user"`` 为用户创建，``"fork"`` 为历史分支，
            ``"delegate_task"`` 为委派子任务。
        parent_task_id: 父任务标识，仅委派子任务有值。
        parent_run_id: 父轮次标识，仅委派子任务有值。
        delegation_id: 所属委派标识，仅委派子任务有值。
        context_window_total: 最后一个 Run 使用的模型上下文窗口上限 token，可能为 None。
        created_at: 创建时间文本。
        updated_at: 更新时间文本。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    task_id: int
    workspace_id: int
    title: str
    extra: dict[str, object] | None = None
    execution_status: str | None = None
    task_type: str = "user"
    fork_available: bool = True
    parent_task_id: int | None = None
    parent_run_id: int | None = None
    delegation_id: int | None = None
    context_window_total: int | None = None
    created_at: str
    updated_at: str

    @classmethod
    def from_record(
        cls,
        record: TaskRecord,
        *,
        context_window_total: int | None = None,
        fork_available: bool = True,
    ) -> "TaskResponse":
        """从 ``TaskRecord`` 值对象构造响应模型。

        把领域值对象前向映射为 API 响应模型，避免 ``**to_dict()`` 因字典值类型
        被推断为 ``str | None`` 而与必填 ``str`` 字段冲突（mypy 报错），同时消除
        重复的字段拆解逻辑。``context_window_total`` 为 Task 持久化的最后一个 Run
        上下文窗口事实，API 层只负责显式映射。

        参数:
            record: 待转换的任务记录。
            context_window_total: 最后一个 Run 使用的模型上下文窗口上限。

        返回:
            与记录字段对齐的 ``TaskResponse`` 实例。

        异常:
            无。

        副作用:
            无。
        """
        return cls(
            task_id=record.id,
            workspace_id=record.workspace_id,
            title=record.title,
            extra=record.extra,
            execution_status=record.execution_status,
            task_type=record.task_type,
            fork_available=fork_available,
            parent_task_id=record.parent_task_id,
            parent_run_id=record.parent_run_id,
            delegation_id=record.delegation_id,
            context_window_total=context_window_total,
            created_at=to_text(record.created_at),
            updated_at=to_text(record.updated_at),
        )
