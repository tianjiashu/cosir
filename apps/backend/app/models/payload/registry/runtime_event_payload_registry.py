"""Runtime event payload model registry."""

from collections.abc import Mapping

from app.models.enums.event_type import EventType
from app.models.payload.final_response_payload import FinalResponsePayload
from app.models.payload.human_input_received_payload import HumanInputReceivedPayload
from app.models.payload.human_input_requested_payload import HumanInputRequestedPayload
from app.models.payload.model_completed_payload import ModelCompletedPayload
from app.models.payload.model_failed_payload import ModelFailedPayload
from app.models.payload.model_output_delta_payload import ModelOutputDeltaPayload
from app.models.payload.model_requested_payload import ModelRequestedPayload
from app.models.payload.model_thinking_delta_payload import ModelThinkingDeltaPayload
from app.models.payload.observation_added_payload import ObservationAddedPayload
from app.models.payload.run_cancelled_payload import RunCancelledPayload
from app.models.payload.run_failed_payload import RunFailedPayload
from app.models.payload.run_finished_payload import RunFinishedPayload
from app.models.payload.run_started_payload import RunStartedPayload
from app.models.payload.runtime_event_payload import RuntimeEventPayload
from app.models.payload.step_started_payload import StepStartedPayload
from app.models.payload.tool_call_finished_payload import ToolCallFinishedPayload
from app.models.payload.tool_call_started_payload import ToolCallStartedPayload

EVENT_PAYLOAD_MODELS: Mapping[EventType, type[RuntimeEventPayload]] = {
    EventType.RUN_STARTED: RunStartedPayload,
    EventType.RUN_FAILED: RunFailedPayload,
    EventType.RUN_CANCELLED: RunCancelledPayload,
    EventType.RUN_FINISHED: RunFinishedPayload,
    EventType.STEP_STARTED: StepStartedPayload,
    EventType.MODEL_REQUESTED: ModelRequestedPayload,
    EventType.MODEL_OUTPUT_DELTA: ModelOutputDeltaPayload,
    EventType.MODEL_THINKING_DELTA: ModelThinkingDeltaPayload,
    EventType.MODEL_COMPLETED: ModelCompletedPayload,
    EventType.MODEL_FAILED: ModelFailedPayload,
    EventType.TOOL_CALL_STARTED: ToolCallStartedPayload,
    EventType.TOOL_CALL_FINISHED: ToolCallFinishedPayload,
    EventType.OBSERVATION_ADDED: ObservationAddedPayload,
    EventType.FINAL_RESPONSE: FinalResponsePayload,
    EventType.HUMAN_INPUT_REQUESTED: HumanInputRequestedPayload,
    EventType.HUMAN_INPUT_RECEIVED: HumanInputReceivedPayload,
}
