from pydantic import BaseModel

from app.models.task_record import TaskRecord
from app.utils.datetime_utils import to_text


class TaskResponse(BaseModel):
    """校验并序列化任务状态响应（与 ``TaskRecord.to_dict()`` 对齐）。

    参数:
        task_id: 任务标识。
        workspace_id: 所属工作区标识。
        agent_id: 驱动该任务的 agent 标识。
        title: 任务标题。
        status: 任务生命周期状态。
        execution_status: 派生执行状态，可能为 None。
        task_type: 任务类型，``"user"`` 为用户创建，``"delegation"`` 为委派子任务。
        parent_task_id: 父任务标识，仅委派子任务有值。
        parent_turn_id: 父轮次标识，仅委派子任务有值。
        delegation_id: 所属委派标识，仅委派子任务有值。
        context_usage_used: 最近一次上下文窗口已用 token（运行时回写，可能为 None）。
        context_window_total: 该任务模型的上下文窗口上限 token（由 resolve_context_window
            动态计算，可能为 None）。
        created_at: 创建时间文本。
        updated_at: 更新时间文本。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    task_id: str
    workspace_id: str
    agent_id: str
    title: str
    status: str
    execution_status: str | None = None
    task_type: str = "user"
    parent_task_id: str | None = None
    parent_turn_id: str | None = None
    delegation_id: str | None = None
    context_usage_used: int | None = None
    context_window_total: int | None = None
    created_at: str
    updated_at: str

    @classmethod
    def from_record(
        cls,
        record: TaskRecord,
        *,
        context_window_total: int | None = None,
    ) -> "TaskResponse":
        """从 ``TaskRecord`` 值对象构造响应模型。

        把领域值对象前向映射为 API 响应模型，避免 ``**to_dict()`` 因字典值类型
        被推断为 ``str | None`` 而与必填 ``str`` 字段冲突（mypy 报错），同时消除
        重复的字段拆解逻辑。``context_window_total`` 为 API 层动态计算的上下文窗口
        上限，不来自 ``TaskRecord``（record 仅持有已用 token），故以关键字参数注入。

        参数:
            record: 待转换的任务记录。
            context_window_total: 该任务模型经 ``resolve_context_window`` 计算得到的
                上下文窗口上限 token；无法解析时为 None。

        返回:
            与记录字段对齐的 ``TaskResponse`` 实例。

        异常:
            无。

        副作用:
            无。
        """
        return cls(
            task_id=record.task_id,
            workspace_id=record.workspace_id,
            agent_id=record.agent_id,
            title=record.title,
            status=record.status,
            execution_status=record.execution_status,
            task_type=record.task_type,
            parent_task_id=record.parent_task_id,
            parent_turn_id=record.parent_turn_id,
            delegation_id=record.delegation_id,
            context_usage_used=record.context_usage_used,
            context_window_total=context_window_total,
            created_at=to_text(record.created_at),
            updated_at=to_text(record.updated_at),
        )
