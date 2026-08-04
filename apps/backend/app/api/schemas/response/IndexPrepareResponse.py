from pydantic import BaseModel


class IndexPrepareResponse(BaseModel):
    """校验并序列化 workspace 索引准备结果响应。

    参数:
        workspace_id: 所属 workspace 标识。
        ready: 索引是否就绪（False 表示降级到文件搜索）。
        state: 归一化状态（ready / failed / unavailable）。
        action_taken: 就绪路径动作（init / sync / none）。
        files_changed: 变更文件数。
        duration_ms: 准备耗时（毫秒）。
        degraded_reason: 降级原因（就绪时为 None）。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    workspace_id: str
    ready: bool
    state: str
    action_taken: str
    files_changed: int
    duration_ms: int
    degraded_reason: str | None = None
