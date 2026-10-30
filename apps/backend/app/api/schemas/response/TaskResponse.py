from pydantic import BaseModel

from app.models.task_record import TaskRecord
from app.utils.datetime_utils import to_text


class TaskResponse(BaseModel):
    """校验并序列化任务状态响应。

    Task 不绑定 Agent：本响应不含 agent_id，Agent 身份固化在各 Run 上。不下发 ``current_run_id``
    （后端的当前 Run 指针，对话界面的 Run 身份只在 snapshot 里供诊断），也不下发执行状态：
    界面的 Task 状态只来自 ``GET /task-status/stream``，不在这里留第二个读取口。

    参数:
        task_id: 任务标识。
        workspace_id: 所属工作区标识。
        title: 任务标题。
        task_type: 任务类型，``"user"`` 为用户创建，``"fork"`` 为历史分支，
            ``"delegate_task"`` 为委派子任务。
        parent_task_id: 父任务标识，仅委派子任务有值。
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
    task_type: str = "user"
    parent_task_id: int | None = None
    created_at: str
    updated_at: str

    @classmethod
    def from_record(
        cls,
        record: TaskRecord,
    ) -> "TaskResponse":
        """从 ``TaskRecord`` 值对象构造响应模型。

        把领域值对象前向映射为 API 响应模型，避免 ``**to_dict()`` 因字典值类型
        被推断为 ``str | None`` 而与必填 ``str`` 字段冲突（mypy 报错），同时消除
        重复的字段拆解逻辑。

        参数:
            record: 待转换的任务记录。

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
            task_type=record.task_type,
            parent_task_id=record.parent_task_id,
            created_at=to_text(record.created_at),
            updated_at=to_text(record.updated_at),
        )
