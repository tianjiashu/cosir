"""workspace 准备触发接口的响应模型。"""

from pydantic import BaseModel


class WorkspacePrepareResponse(BaseModel):
    """POST /workspaces/{workspace_id}/events/prepare 的响应。

    触发一次 workspace 准备（当前为 CodeGraph 索引就绪）并立即返回；
    准备进度经 ``GET /workspaces/{workspace_id}/events/stream`` SSE 推送。

    属性:
        workspace_id: 触发准备的 workspace 标识。
        ready: 即时快照中 Kernel 是否已就绪（True / False）。
        state: 即时状态快照，取值 ``ready`` / ``failed`` / ``unavailable`` /
            ``unreachable`` / ``timeout``；任何非 ``ready`` 值均表示未就绪，前端应
            降级到文件搜索但任务可继续。同步阻塞路径下通常为 ``accepted`` 初值，
            终态以 SSE 事件为准。
        action_taken: 采取的动作（``init`` / ``sync`` / ``none``）。
        files_changed: 索引变更文件数（ready 时有效）。
        duration_ms: 准备耗时（毫秒）。
        degraded_reason: 降级原因（非 ready 时有效；脱敏，不记录 secret）。
    """

    workspace_id: str
    ready: bool
    state: str
    action_taken: str = "none"
    files_changed: int = 0
    duration_ms: int = 0
    degraded_reason: str | None = None
