from pydantic import BaseModel


class DeleteTaskResponse(BaseModel):
    """任务删除结果。

    ``deleted_task_ids`` 是实际删除的全部 Task（含委派子 Agent、Team 节点与一并删除的 fork），
    前端据此丢弃按 taskId 分桶的界面状态、判断当前页是否已被删。
    """

    task_id: int
    deleted: bool
    deleted_task_ids: list[int]
