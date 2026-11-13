import { requestJson, type RequestOptions } from "@/lib/http/client";

export type ContextMessage = {
  id: number | null;
  session_id: number;
  /** 所在回合（系统提示词为 0）。 */
  turn: number;
  /** 来自分叉继承的历史（没有本地运行）。 */
  inherited: boolean;
  sequence: number;
  include_in_context: boolean;
  created_at: string;
  updated_at: string;
  role: string;
  reasoning_content?: string | null;
  content: unknown;
  tool_calls?: Array<Record<string, unknown>> | null;
  usage_metadata?: Record<string, unknown> | null;
  tool_call_id?: string | null;
  status?: string | null;
};

/** 所选回合所属运行的状态事实；属于整个运行，同一运行的其他回合看到的是同一份。 */
export type ContextTurnRun = {
  status: string;
  end_reason: string | null;
  error_code: string | null;
  usage: Record<string, number | null> | null;
  started_at: string;
  updated_at: string;
};

export type ContextObservation = {
  messages: ContextMessage[];
  /** 只有按回合查询、且这一回合有本地运行时才有。 */
  run: ContextTurnRun | null;
};

export type ContextMessageQuery = {
  sessionId?: number;
  turn?: number;
};

/** 读取 Session 的上下文记录，或只读一个回合；Session 必填（观测按 Session 与回合进入，前端看不到运行）。 */
export function getContextMessages(query: ContextMessageQuery, options?: RequestOptions) {
  const params = new URLSearchParams();
  if (query.sessionId !== undefined) params.set("session_id", String(query.sessionId));
  if (query.turn !== undefined) params.set("turn", String(query.turn));
  return requestJson<ContextObservation>(`/context-messages?${params.toString()}`, options);
}
