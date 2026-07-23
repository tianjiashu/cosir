from pydantic import BaseModel


class TurnResponse(BaseModel):
    """校验并序列化轮次状态响应（与 ``TurnRecord.to_dict()`` 对齐）。

    参数:
        turn_id: 轮次标识。
        task_id: 所属任务标识。
        input_text: 本轮用户输入。
        status: 轮次状态。
        end_reason: 终态原因，可能为 None。
        response_text: Agent 回复文本，可能为 None。
        agent_id: 驱动该轮次的 agent 标识，可能为 None。
        created_at: 创建时间文本。
        updated_at: 更新时间文本。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    turn_id: str
    task_id: str
    input_text: str
    status: str
    end_reason: str | None = None
    response_text: str | None = None
    agent_id: str | None = None
    created_at: str
    updated_at: str
