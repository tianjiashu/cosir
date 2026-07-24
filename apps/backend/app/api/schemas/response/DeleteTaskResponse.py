from pydantic import BaseModel


class DeleteTaskResponse(BaseModel):
    """校验并序列化任务删除结果响应。

    参数:
        task_id: 被删除的任务标识。
        deleted: 是否删除成功。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    task_id: str
    deleted: bool
