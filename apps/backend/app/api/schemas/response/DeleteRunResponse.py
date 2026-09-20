from pydantic import BaseModel


class DeleteRunResponse(BaseModel):
    """校验并序列化单个 run 删除结果响应。

    参数:
        task_id: run 所属任务标识。
        run_id: 被删除的 Conversation Run 标识。
        deleted: 是否删除成功。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    task_id: int
    run_id: int
    deleted: bool
