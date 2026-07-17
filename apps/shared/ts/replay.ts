/**
 * Agent Replay 共享类型。
 *
 * 该文件只描述前后端共享的只读响应契约，不承载投影规则。
 */

export type ReplayNodeType =
  | "user_message"
  | "run_started"
  | "workflow_step"
  | "model_call"
  | "model_output"
  | "tool_call"
  | "tool_result"
  | "approval_wait"
  | "approval_decision"
  | "checkpoint"
  | "resume"
  | "artifact"
  | "final_response"
  | "run_completed"
  | "run_failed"
  | "run_cancelled";

export interface ReplayNode {
  replay_node_id: string;
  trace_id: string;
  run_id: string;
  task_id: string;
  sequence_start: number;
  sequence_end: number;
  node_type: ReplayNodeType;
  title: string;
  status: string;
  summary: string;
  related_span_ids: string[];
  related_event_ids: string[];
  related_artifact_ids: string[];
  checkpoint_id: string;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
  payload?: Record<string, unknown>;
  debug?: Record<string, unknown>;
}

export interface ReplayTimeline {
  trace_id: string;
  run_id: string;
  task_id: string;
  nodes: ReplayNode[];
  debug?: Record<string, unknown>;
}

export type ReplayNodeDetail = ReplayNode & {
  payload: Record<string, unknown>;
  debug?: Record<string, unknown>;
};
