"""按事件类型分派到对应 planner 的唯一投影入口。"""

from collections.abc import Callable, Sequence
from typing import Any

from app.assistant_transport.projection.message_event_projection import (
    plan_assistant_part_closed,
    plan_assistant_text_delta,
)
from app.assistant_transport.projection.run_event_projection import (
    plan_run_initialized,
    plan_run_result_updated,
    plan_run_status_changed,
)
from app.assistant_transport.projection.task_event_projection import plan_task_title_updated
from app.assistant_transport.projection.tool_call_event_projection import (
    plan_tool_call_created,
    plan_tool_call_status_changed,
    plan_tool_calls_settled,
)
from app.assistant_transport.projection.tool_runtime_event_projection import (
    plan_tool_call_runtime_update,
)
from app.assistant_transport.projection.user_input_projection import plan_user_input_consumed
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.core.events import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    RunInitializedEvent,
    RunResultUpdatedEvent,
    RunStatusChangedEvent,
    TaskTitleUpdatedEvent,
    ToolCallCreatedEvent,
    ToolCallRuntimeUpdateEvent,
    ToolCallsSettledEvent,
    ToolCallStatusChangedEvent,
    UserInputConsumedEvent,
    WorkflowEvent,
)

_Planner = Callable[[Any, ConversationStateSnapshot], Sequence[ConversationStateMutation]]

_PLANNERS: dict[type, _Planner] = {
    RunInitializedEvent: plan_run_initialized,
    UserInputConsumedEvent: plan_user_input_consumed,
    RunStatusChangedEvent: plan_run_status_changed,
    RunResultUpdatedEvent: plan_run_result_updated,
    AssistantTextDeltaEvent: plan_assistant_text_delta,
    AssistantPartClosedEvent: plan_assistant_part_closed,
    ToolCallCreatedEvent: plan_tool_call_created,
    ToolCallStatusChangedEvent: plan_tool_call_status_changed,
    ToolCallsSettledEvent: plan_tool_calls_settled,
    ToolCallRuntimeUpdateEvent: plan_tool_call_runtime_update,
    TaskTitleUpdatedEvent: plan_task_title_updated,
}


def plan_event(
    event: WorkflowEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """把一条已校验的事件投影为 snapshot mutations；不修改 ``state``。

    ``WorkflowEvent`` 联合中的每个类型都必须在 ``_PLANNERS`` 注册，漏注册以 ``KeyError``
    暴露，不静默跳过。
    """

    return _PLANNERS[type(event)](event, state)
