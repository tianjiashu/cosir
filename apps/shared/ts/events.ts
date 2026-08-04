/**
 * 后端运行时事件类型定义。
 *
 * 本文件由 `scripts/generate_runtime_event_ts.py` 从后端 Pydantic payload 模型生成。
 * 不要手动修改；请先更新 `apps/backend/app/models/payload/` 后重新生成。
 *
 * @module shared/events
 */

/** 后端发出的运行时事件类型。 */
export type RuntimeEventType =
  | "run_started"
  | "run_failed"
  | "run_cancelled"
  | "run_finished"
  | "step_started"
  | "model_requested"
  | "model_output_delta"
  | "model_thinking_delta"
  | "model_completed"
  | "model_failed"
  | "tool_call_started"
  | "tool_call_finished"
  | "observation_added"
  | "final_response"
  | "human_input_requested"
  | "human_input_received"
  | "workspace_preparing"
  | "workspace_ready"
  | "workspace_degraded";

/** 所有运行时事件 payload 都是 JSON object。 */
export type RuntimeEventPayloadObject = Record<string, unknown>;

export interface ModelToolCallPayload extends RuntimeEventPayloadObject {
  tool_name: string;
  arguments?: Record<string, unknown>;
  call_id?: string;
}

export interface RunStartedPayload extends RuntimeEventPayloadObject {
  status: "running";
  agent_id: string;
}

export interface RunFailedPayload extends RuntimeEventPayloadObject {
  error: string;
  status?: "failed" | null;
  message?: string | null;
  step_id?: string | null;
  requested_agent_id?: string | null;
  task_agent_id?: string | null;
  tool_name?: string | null;
  langfuse_trace_id?: string | null;
}

export interface RunCancelledPayload extends RuntimeEventPayloadObject {
  status: "cancelled";
  step_id?: string | null;
  error?: string | null;
  langfuse_trace_id?: string | null;
}

export interface RunFinishedPayload extends RuntimeEventPayloadObject {
  status: "completed";
  step_id?: string | null;
  duration_ms?: number;
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  cache_hit_tokens?: number;
  cache_miss_tokens?: number;
  reasoning_tokens?: number;
  langfuse_trace_id?: string | null;
}

export interface StepStartedPayload extends RuntimeEventPayloadObject {
  step_id: string;
  kind: string;
  index: number;
}

export interface ModelRequestedPayload extends RuntimeEventPayloadObject {
  step_id: string;
  message_count: number;
}

export interface ModelOutputDeltaPayload extends RuntimeEventPayloadObject {
  step_id: string;
  text: string;
}

export interface ModelThinkingDeltaPayload extends RuntimeEventPayloadObject {
  step_id: string;
  text: string;
}

export interface ModelCompletedPayload extends RuntimeEventPayloadObject {
  step_id: string;
  text: string;
  tool_calls: ModelToolCallPayload[];
}

export interface ModelFailedPayload extends RuntimeEventPayloadObject {
  error: string;
  step_id?: string | null;
  status?: "failed" | null;
}

export interface ToolCallStartedPayload extends RuntimeEventPayloadObject {
  tool_name: string;
  step_id?: string | null;
  tool_call_id?: string | null;
  arguments?: Record<string, unknown> | null;
  display?: Record<string, unknown> | null;
}

export interface ToolCallFinishedPayload extends RuntimeEventPayloadObject {
  step_id: string;
  tool_name: string;
  status: "success" | "error";
  tool_call_id: string;
  content?: string | null;
  error?: string;
  reason?: string;
  retryable?: boolean;
  data?: Record<string, unknown>;
}

export interface ObservationAddedPayload extends RuntimeEventPayloadObject {
  tool_name: string;
  status: "success" | "error";
  step_id?: string | null;
  tool_call_id?: string | null;
  summary?: string | null;
  details?: Record<string, unknown>;
}

export interface FinalResponsePayload extends RuntimeEventPayloadObject {
  text: string;
  step_id: string;
  status: "completed";
}

export interface HumanInputRequestedPayload extends RuntimeEventPayloadObject {
  prompt: string;
  request_id?: string | null;
  details?: Record<string, unknown>;
}

export interface HumanInputReceivedPayload extends RuntimeEventPayloadObject {
  request_id?: string | null;
  response?: string | null;
  details?: Record<string, unknown>;
}

export interface WorkspacePreparingPayload extends RuntimeEventPayloadObject {
  workspace_path: string;
}

export interface WorkspaceReadyPayload extends RuntimeEventPayloadObject {
  workspace_path: string;
  action_taken: "init" | "sync";
  files_changed: number;
  duration_ms: number;
}

export interface WorkspaceDegradedPayload extends RuntimeEventPayloadObject {
  workspace_path: string;
  state: string;
  degraded_reason: string;
}

/** event_type 到 payload 类型的映射。 */
export interface RuntimeEventPayloadMap {
  run_started: RunStartedPayload;
  run_failed: RunFailedPayload;
  run_cancelled: RunCancelledPayload;
  run_finished: RunFinishedPayload;
  step_started: StepStartedPayload;
  model_requested: ModelRequestedPayload;
  model_output_delta: ModelOutputDeltaPayload;
  model_thinking_delta: ModelThinkingDeltaPayload;
  model_completed: ModelCompletedPayload;
  model_failed: ModelFailedPayload;
  tool_call_started: ToolCallStartedPayload;
  tool_call_finished: ToolCallFinishedPayload;
  observation_added: ObservationAddedPayload;
  final_response: FinalResponsePayload;
  human_input_requested: HumanInputRequestedPayload;
  human_input_received: HumanInputReceivedPayload;
  workspace_preparing: WorkspacePreparingPayload;
  workspace_ready: WorkspaceReadyPayload;
  workspace_degraded: WorkspaceDegradedPayload;
}

/** SSE 传输的运行时事件信封，对应后端 `RuntimeEvent.to_dict()`。 */
export interface RuntimeEventEnvelope<T extends RuntimeEventType = RuntimeEventType> {
  /** 唯一的事件标识符（UUID）。 */
  event_id: string;
  /** 稳定的、机器可读的事件类型。 */
  event_type: T;
  /** 关联的任务标识符。 */
  task_id: string;
  /** 关联的轮次标识符。 */
  turn_id?: string | null;
  /** 当前单次运行流内的排序号；不是 task 级持久序号。 */
  sequence?: number;
  /** 可选的用户可读消息标识符。 */
  message_id?: string | null;
  /** 可选的工具调用标识符。 */
  tool_call_id?: string | null;
  /** 事件创建时的 UTC 时间戳（ISO-8601）。 */
  created_at: string;
  /** 因 event_type 而异的载荷字典。 */
  payload: RuntimeEventPayloadMap[T];
}

/** 后端 SSE 运行时事件联合类型。 */
export type RuntimeEvent = {
  [T in RuntimeEventType]: RuntimeEventEnvelope<T>;
}[RuntimeEventType];
