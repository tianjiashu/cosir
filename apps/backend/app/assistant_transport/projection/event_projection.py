"""按事件类型分派到对应 planner 的唯一投影入口。"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from app.assistant_transport.projection.message_event_projection import (
    plan_assistant_part_closed,
    plan_assistant_text_delta,
)
from app.assistant_transport.projection.model_activity_projection import plan_model_activity
from app.assistant_transport.projection.run_event_projection import (
    plan_run_initialized,
    plan_run_result_updated,
    plan_run_status_changed,
)
from app.assistant_transport.projection.session_event_projection import plan_session_title_updated
from app.assistant_transport.projection.tool_call_event_projection import (
    plan_tool_call_created,
    plan_tool_call_intent,
    plan_tool_call_status_changed,
)
from app.assistant_transport.projection.tool_runtime_event_projection import (
    plan_tool_call_runtime_update,
)
from app.assistant_transport.projection.user_input_projection import plan_input_consumed
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.core.events import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    InputConsumedEvent,
    ModelActivityEvent,
    RunInitializedEvent,
    RunResultUpdatedEvent,
    RunStatusChangedEvent,
    SessionTitleUpdatedEvent,
    ToolCallCreatedEvent,
    ToolCallIntentEvent,
    ToolCallRuntimeUpdateEvent,
    ToolCallStatusChangedEvent,
    WorkflowEvent,
)
from app.core.tool.presentation import ToolPresentation

_Planner = Callable[[Any, ConversationStateSnapshot], Sequence[ConversationStateMutation]]

_PLANNERS: dict[type, _Planner] = {
    ModelActivityEvent: plan_model_activity,
    RunInitializedEvent: plan_run_initialized,
    InputConsumedEvent: plan_input_consumed,
    RunStatusChangedEvent: plan_run_status_changed,
    RunResultUpdatedEvent: plan_run_result_updated,
    AssistantTextDeltaEvent: plan_assistant_text_delta,
    AssistantPartClosedEvent: plan_assistant_part_closed,
    ToolCallIntentEvent: plan_tool_call_intent,
    ToolCallStatusChangedEvent: plan_tool_call_status_changed,
    ToolCallRuntimeUpdateEvent: plan_tool_call_runtime_update,
    SessionTitleUpdatedEvent: plan_session_title_updated,
}


def plan_event(
    event: WorkflowEvent,
    state: ConversationStateSnapshot,
    presentations: Mapping[str, ToolPresentation],
) -> Sequence[ConversationStateMutation]:
    """把一条已校验的事件投影为 snapshot mutations；不修改 ``state``。

    ``presentations`` 是当前注册表的工具呈现类别（``presentation_catalog``），只有建立工具调用时
    用到，所以那一种事件单独分派；其余类型都必须在 ``_PLANNERS`` 注册，漏注册以 ``KeyError``
    暴露，不静默跳过。
    """

    if isinstance(event, ToolCallCreatedEvent):
        return plan_tool_call_created(event, state, presentations)
    return _PLANNERS[type(event)](event, state)
