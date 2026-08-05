from pydantic import BaseModel

from app.models.task_record import TaskRecord
from app.utils.datetime_utils import to_text


class TaskResponse(BaseModel):
    """校验并序列化任务状态响应（与 ``TaskRecord.to_dict()`` 对齐）。

    参数:
        task_id: 任务标识。
        workspace_id: 所属工作区标识。
        agent_id: 驱动该任务的 agent 标识。
        input_text: 任务的首条用户输入。
        title: 任务标题。
        last_message_preview: 最近一条消息预览。
        latest_turn_id: 最近一轮标识，可能为 None。
        status: 任务生命周期状态。
        execution_status: 派生执行状态，可能为 None。
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
    input_text: str
    title: str
    last_message_preview: str
    latest_turn_id: str | None = None
    status: str
    execution_status: str | None = None
    created_at: str
    updated_at: str

    @classmethod
    def from_record(cls, record: TaskRecord) -> "TaskResponse":
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
            task_id=record.task_id,
            workspace_id=record.workspace_id,
            agent_id=record.agent_id,
            input_text=record.input_text,
            title=record.title,
            last_message_preview=record.last_message_preview,
            latest_turn_id=record.latest_turn_id,
            status=record.status,
            execution_status=record.execution_status,
            created_at=to_text(record.created_at),
            updated_at=to_text(record.updated_at),
        )
