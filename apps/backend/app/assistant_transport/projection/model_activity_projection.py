"""模型活动只投影到运行中的当前活动；结束旧步骤不能清除新的步骤。"""

from app.assistant_transport.state.conversation_state_activity import ModelActivity
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.core.events import ModelActivityEvent


def plan_model_activity(
    event: ModelActivityEvent, state: ConversationStateSnapshot
) -> list[ConversationStateMutation]:
    """更新瞬时活动，不创建消息、不保存推理正文。"""
    activity = state["activity"]
    if activity is None or activity["status"] != "running":
        return []
    current = activity["modelActivity"]
    if event.phase is None and (current is None or current["stepId"] != event.step_id):
        return []
    value: ModelActivity | None = (
        None if event.phase is None else {"stepId": event.step_id, "phase": event.phase}
    )
    return (
        []
        if value == current
        else [ConversationStateMutation("set", ("activity", "modelActivity"), value)]
    )
