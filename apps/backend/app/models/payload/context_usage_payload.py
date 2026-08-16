"""上下文窗口占用事件的 payload 值对象。"""

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ContextUsagePayload(RuntimeEventPayload):
    """上下文窗口占用事件 payload（输入侧 token 估算）。

    携带当前上下文窗口已用 token 与实际上限，供前端渲染上下文占用圆环。
    数据基于 RuntimeContext.messages 本地估算，不依赖模型 usage_metadata，
    因此 turn 中途取消也不丢。
    """

    used_tokens: int
    total_tokens: int
