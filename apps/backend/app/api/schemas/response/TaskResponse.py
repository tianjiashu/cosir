from pydantic import BaseModel


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
