from pydantic import BaseModel


class IndexPrepareResponse(BaseModel):
    """校验并序列化 workspace 索引准备结果响应。

    参数:
        workspace_id: 所属 workspace 标识。
        ready: 索引是否就绪（False 表示降级到文件搜索）。
        state: 归一化状态（ready / failed / unavailable / unreachable / timeout）。
            ready 表示索引就绪；failed 表示就绪路径失败；unavailable 表示 Kernel 不可用
            （设计明确的降级场景）；unreachable 表示 prepare 前健康快检未通过；
            timeout 表示 prepare 超过总超时上限而被强制降级。
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
