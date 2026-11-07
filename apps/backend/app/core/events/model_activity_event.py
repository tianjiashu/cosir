"""模型调用边界的瞬时事实；不承载推理内容或调度状态。"""

from typing import Literal

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope

ModelActivityPhase = Literal["waiting", "reasoning", "responding", "tool_call"]


class ModelActivityEvent(WorkflowEventEnvelope):
    """同一步开始等待、输出通道变化或调用退出；None 表示该步已退出。"""

    type: Literal["model_activity"] = "model_activity"
    step_id: str
    phase: ModelActivityPhase | None
