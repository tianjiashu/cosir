import { requestJson, type RequestOptions } from "@/lib/http/client";

export type ObservedMessage = {
  id: number | null;
  session_id: number;
  /** 所在回合（系统提示词为 0）。 */
  turn: number;
  /** 来自分叉继承的历史（没有本地运行）。 */
  inherited: boolean;
  sequence: number;
  include_in_context: boolean;
  /** 是否显示在对话界面；与是否进模型上下文独立（内部通知进模型但不显示）。 */
  project_to_transport: boolean;
  /** 被撤回的行：既不进模型也不显示，只留给观测。 */
  withdrawn: boolean;
  created_at: string;
  updated_at: string;
  role: string;
  /** user 行的来源种类；进模型时都是 user 角色，区分来源靠它。 */
  kind?: string | null;
  reasoning_content?: string | null;
  content: unknown;
  tool_calls?: Array<Record<string, unknown>> | null;
  usage_metadata?: Record<string, unknown> | null;
  tool_call_id?: string | null;
  /** 工具结果行与回答某次调用的 user 行对应的工具名；由后端给，发起调用的行可能不在所选回合里。 */
  tool_name?: string | null;
  status?: string | null;
};

/** 一个运行节点：运行在 `at` 从 `previous_status` 到达 `status`；创建那一条没有来源。 */
export type ObservedStatusChange = {
  at: string;
  status: string;
  previous_status: string | null;
  previous_at: string | null;
  end_reason: string | null;
  /** 这个运行目前最后一次迁移：运行的结局与用量挂在它上面展示。 */
  latest: boolean;
};

/** 一个运行的状态事实与运行节点；结局与用量属于整个运行。按回合查询时 `changes` 只含这一回合时间窗里的节点。 */
export type ObservedRun = {
  status: string;
  end_reason: string | null;
  error_code: string | null;
  usage: Record<string, number | null> | null;
  started_at: string;
  updated_at: string;
  changes: ObservedStatusChange[];
};

export type SessionObservation = {
  session_title: string;
  /** Session 里最大的回合号，不随所选回合变化。 */
  last_turn: number;
  messages: ObservedMessage[];
  /** 按回合查询时至多一个；继承来的回合没有。 */
  runs: ObservedRun[];
};

export type SessionObservationQuery = {
  sessionId: number;
  turn?: number;
};

/** 读取 Session 的全部消息行与运行节点，或只读一个回合（观测按 Session 与回合进入，前端看不到运行号）。 */
export function getSessionObservation(query: SessionObservationQuery, options?: RequestOptions) {
  const suffix = query.turn === undefined ? "" : `?turn=${query.turn}`;
  return requestJson<SessionObservation>(`/sessions/${query.sessionId}/observation${suffix}`, options);
}
