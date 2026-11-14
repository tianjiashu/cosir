from pydantic import BaseModel


class DeleteSessionResponse(BaseModel):
    """会话删除结果。

    ``deleted_session_ids`` 是实际删除的全部 Session（含委派子 Agent 与一并删除的 fork），
    前端据此丢弃按 sessionId 分桶的界面状态、判断当前页是否已被删。
    """

    session_id: int
    deleted: bool
    deleted_session_ids: list[int]
