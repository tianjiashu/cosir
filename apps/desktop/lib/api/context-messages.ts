import { requestJson, type RequestOptions } from "@/lib/http/client";

export type ContextMessage = {
  id: number | null;
  task_id: number;
  run_id: number | null;
  sequence: number;
  include_in_context: boolean;
  created_at: string;
  updated_at: string;
  role: string;
  content: unknown;
  tool_calls?: Array<Record<string, unknown>> | null;
  usage_metadata?: Record<string, unknown> | null;
  tool_call_id?: string | null;
  status?: string | null;
};

export type ContextMessageQuery = {
  taskId?: number;
  runId?: number;
};

export function getContextMessages(query: ContextMessageQuery, options?: RequestOptions) {
  const params = new URLSearchParams();
  if (query.taskId !== undefined) params.set("task_id", String(query.taskId));
  if (query.runId !== undefined) params.set("run_id", String(query.runId));
  return requestJson<ContextMessage[]>(`/context-messages?${params.toString()}`, options);
}
