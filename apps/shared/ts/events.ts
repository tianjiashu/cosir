/**
 * 后端运行时事件类型定义。
 *
 * 与后端 `app/events/types.py::RuntimeEvent.to_dict()` 输出保持一致，
 * 作为前后端共享的 SSE 事件契约事实源。
 *
 * @module shared/events
 */

/** 后端发出的 14 种运行时事件类型，与 `docs/desktop-client-development-plan.md` §5.3 一一对应。 */
export type RuntimeEventType =
  | "run_started"
  | "step_started"
  | "model_output_delta"
  | "tool_call_requested"
  | "tool_call_started"
  | "tool_call_finished"
  | "observation_added"
  | "final_response"
  | "run_finished"
  | "run_failed"
  | "run_cancelled";

/** SSE 传输的完整运行时事件结构，对应后端 `RuntimeEvent.to_dict()`。 */
export interface RuntimeEvent {
  /** 唯一的事件标识符（UUID）。 */
  event_id: string;
  /** 稳定的、机器可读的事件类型（14 种之一）。 */
  event_type: RuntimeEventType;
  /** 关联的任务标识符。 */
  task_id: string;
  /** 关联的轮次标识符。 */
  turn_id?: string | null;
  /** 同一 task 下稳定递增的排序号。 */
  sequence?: number;
  /** 可选的用户可读消息标识符。 */
  message_id?: string | null;
  /** 可选的工具调用标识符。 */
  tool_call_id?: string | null;
  /** 事件创建时的 UTC 时间戳（ISO-8601）。 */
  created_at: string;
  /** 因 event_type 而异的载荷字典。 */
  payload: Record<string, unknown>;
}

// ---------- 各事件载荷子类型 ----------

/** `run_started` 载荷：任务开始运行。 */
export interface RunStartedPayload {
  status: "running";
  agent: Record<string, unknown>;
}

/** `step_started` 载荷：步骤开始。 */
export interface StepStartedPayload {
  step_type: string;
  step_index: number;
}

/** `model_output_delta` 载荷：模型增量输出。 */
export interface ModelOutputDeltaPayload {
  delta: string;
}

/** `tool_call_requested` 载荷：模型请求调用工具（待执行）。 */
export interface ToolCallRequestedPayload {
  tool_name: string;
  arguments: Record<string, unknown>;
}

/** `tool_call_started` 载荷：工具调用开始执行。 */
export interface ToolCallStartedPayload {
  tool_name: string;
}

/** `tool_call_finished` 载荷：工具调用执行完成。 */
export interface ToolCallFinishedPayload {
  tool_name: string;
  status: string;
  error?: string;
}

/** `observation_added` 载荷：观察/结果回填。 */
export interface ObservationAddedPayload {
  tool_name: string;
  status: string;
}

/** `final_response` 载荷：模型最终响应产出。 */
export interface FinalResponsePayload {
  status: "completed";
}

/** `run_finished` 载荷：任务正常完成。 */
export interface RunFinishedPayload {
  status: "completed";
}

/** `run_failed` 载荷：任务失败。 */
export interface RunFailedPayload {
  status: "failed";
  error: string;
}

/** `run_cancelled` 载荷：任务被取消。 */
export interface RunCancelledPayload {
  status: "cancelled";
}
