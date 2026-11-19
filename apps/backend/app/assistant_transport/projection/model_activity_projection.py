"""模型活动只投影到运行中的当前活动；结束旧步骤不能清除新的步骤。"""

from app.assistant_transport.state.conversation_state_activity import ModelActivity
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.core.events import ModelActivityEvent
from app.utils.datetime_utils import to_text


def plan_model_activity(
    event: ModelActivityEvent, state: ConversationStateSnapshot
) -> list[ConversationStateMutation]:
    """更新瞬时活动，不创建消息、不保存推理正文。"""
    activity = state["activity"]
    if activity is None or activity["status"] != "running":
        return []
    current = activity["modelActivity"]
    same_step = current is not None and current["stepId"] == event.step_id
    if event.phase is None:
        return [ConversationStateMutation("set", ("activity", "modelActivity"), None)] if same_step else []
    # 起点跟模型步：同一步内换阶段沿用，新的一步取这次事件的发生时刻。
    value: ModelActivity = {
        "stepId": event.step_id,
        "phase": event.phase,
        "startedAt": current["startedAt"] if same_step else to_text(event.occurred_at),
    }
    # 同一阶段的重复事件由发布出口按空操作丢弃，这里不再各自比较新旧值。
    return [ConversationStateMutation("set", ("activity", "modelActivity"), value)]
