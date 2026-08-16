from enum import Enum


class EventType(str, Enum):
    """运行时事件类型枚举。

    枚举成员的值即为 SSE ``event:`` 字段与持久化存储中的字符串，
    ``str(event_type)`` 会返回该稳定值，避免散落的字符串字面量产生拼写漂移。
    """

    RUN_STARTED = "run_started"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    RUN_FINISHED = "run_finished"
    STEP_STARTED = "step_started"
    MODEL_REQUESTED = "model_requested"
    MODEL_OUTPUT_DELTA = "model_output_delta"
    MODEL_THINKING_DELTA = "model_thinking_delta"
    MODEL_COMPLETED = "model_completed"
    MODEL_FAILED = "model_failed"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_OUTPUT_DELTA = "tool_output_delta"
    TOOL_CALL_FINISHED = "tool_call_finished"
    OBSERVATION_ADDED = "observation_added"
    FINAL_RESPONSE = "final_response"
    FILE_CHANGE_STABLE = "file_change_stable"
    FILE_CHANGE_UPDATED = "file_change_updated"
    DELEGATION_STARTED = "delegation_started"
    DELEGATION_CHILD_STARTED = "delegation_child_started"
    DELEGATION_FINISHED = "delegation_finished"
    DELEGATION_FAILED = "delegation_failed"
    DELEGATION_CANCELLED = "delegation_cancelled"

    # 以下两个成员为 Human-in-Loop 预留（向用户提问 / 用户回复）。
    # 本轮仅定义、不 emit：审批异步持久化暂缓，待后续接 Human-in-Loop 时再接线。
    HUMAN_INPUT_REQUESTED = "human_input_requested"
    HUMAN_INPUT_RECEIVED = "human_input_received"

    # --- CodeGraph 索引准备阶段事件（第二阶段方案二） ---
    # 只在 turn 执行前 emit：workspace_preparing（开始）→ ready / degraded（结束）。
    WORKSPACE_PREPARING = "workspace_preparing"
    WORKSPACE_READY = "workspace_ready"
    WORKSPACE_DEGRADED = "workspace_degraded"

    # 上下文窗口占用事件：每次模型调用前 emit 当前上下文输入侧 token 占用（本地估算），
    # 用于前端上下文圆环渲染；数据来自 RuntimeContext.messages，turn 取消也不丢。
    CONTEXT_USAGE = "context_usage"

    def __str__(self) -> str:
        """返回事件类型的稳定字符串值。

        参数:
            无。

        返回:
            可用于 SSE、日志与持久化边界的事件类型字面量。

        异常:
            无。

        副作用:
            无。
        """

        return self.value


TERMINAL_EVENT_TYPES = {
    EventType.RUN_FINISHED,
    EventType.RUN_FAILED,
    EventType.RUN_CANCELLED,
}
