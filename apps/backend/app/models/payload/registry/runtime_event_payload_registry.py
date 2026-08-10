"""Runtime event payload model registry."""

from collections.abc import Mapping

from app.models.enums.event_type import EventType
from app.models.payload.file_change_stable_payload import FileChangeStablePayload
from app.models.payload.file_change_updated_payload import FileChangeUpdatedPayload
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
from app.models.payload.tool_output_delta_payload import ToolOutputDeltaPayload
from app.models.payload.workspace_payload.workspace_degraded_payload import WorkspaceDegradedPayload
from app.models.payload.workspace_payload.workspace_preparing_payload import (
    WorkspacePreparingPayload,
)
from app.models.payload.workspace_payload.workspace_ready_payload import WorkspaceReadyPayload

EVENT_PAYLOAD_MODELS: Mapping[EventType, type[RuntimeEventPayload]] = {
    # 以下为前端已语义消费的事件类型（projector / useSSE / useChanges / workspaceEventStore）
    EventType.RUN_STARTED: RunStartedPayload,
    EventType.RUN_FAILED: RunFailedPayload,
    EventType.RUN_CANCELLED: RunCancelledPayload,
    EventType.RUN_FINISHED: RunFinishedPayload,
    EventType.MODEL_OUTPUT_DELTA: ModelOutputDeltaPayload,
    EventType.MODEL_THINKING_DELTA: ModelThinkingDeltaPayload,
    EventType.TOOL_CALL_STARTED: ToolCallStartedPayload,
    EventType.TOOL_OUTPUT_DELTA: ToolOutputDeltaPayload,
    EventType.TOOL_CALL_FINISHED: ToolCallFinishedPayload,
    EventType.FINAL_RESPONSE: FinalResponsePayload,
    EventType.FILE_CHANGE_STABLE: FileChangeStablePayload,
    EventType.FILE_CHANGE_UPDATED: FileChangeUpdatedPayload,
    EventType.WORKSPACE_PREPARING: WorkspacePreparingPayload,
    EventType.WORKSPACE_READY: WorkspaceReadyPayload,
    EventType.WORKSPACE_DEGRADED: WorkspaceDegradedPayload,
    # 以下为前端尚未语义消费的事件类型（后端仍发射并持久化，前端 eventStore 仅全量存储）
    EventType.STEP_STARTED: StepStartedPayload,  # 未消费：前端未渲染 step 边界
    EventType.MODEL_REQUESTED: ModelRequestedPayload,  # 未消费：前端未渲染 model 请求态
    EventType.MODEL_COMPLETED: ModelCompletedPayload,  # 未消费：projector 仅用 delta/final_response 渲染
    EventType.MODEL_FAILED: ModelFailedPayload,  # 未消费：前端未渲染 model 失败态（仅 run_failed 驱动状态）
    EventType.OBSERVATION_ADDED: ObservationAddedPayload,  # 未消费：前端未渲染 observation
    EventType.HUMAN_INPUT_REQUESTED: HumanInputRequestedPayload,  # 未消费：Human-in-Loop 预留，暂无前端处理
    EventType.HUMAN_INPUT_RECEIVED: HumanInputReceivedPayload,  # 未消费：Human-in-Loop 预留，暂无前端处理
}
