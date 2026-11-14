import { readText } from "./display-data-readers";
import type { TransportRunStatus } from "@/lib/assistant/contract";


export type DelegationDisplay = {
  title?: string;
  role?: string;
  childAgentId?: string;
  childSessionId?: number;
  status?: TransportRunStatus;
  /** 子会话的可写根（相对工作区根）；没有即整个工作区。 */
  writableRoot?: string;
};

/** `agent-session-result`：`send_message`与 `session_status`。 */
export type AgentSessionResultDisplay = {
  operation: "send" | "status";
  sessionId: number;
  agentName?: string;
  finalOutput?: string;
  endReason?: string;
};

// 子 Agent 卡片展示的是子 Session 最新 Run 的状态（含 suspended），不是工具调用状态。
const CHILD_AGENT_STATUSES = new Set<TransportRunStatus>([
  "running",
  "suspended",
  "completed",
  "failed",
  "cancelled",
]);

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function positiveId(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : undefined;
}

function childAgentStatus(value: unknown): TransportRunStatus | undefined {
  return typeof value === "string" && CHILD_AGENT_STATUSES.has(value as TransportRunStatus)
    ? value as TransportRunStatus
    : undefined;
}

/** Read the allowlisted delegation projection; prompt/error-shaped fields are ignored. */
export function readDelegationDisplay(value: unknown): DelegationDisplay | null {
  const data = record(value);
  if (!data || data.kind !== "delegation-result") return null;
  const statusValue = data.status === undefined ? undefined : childAgentStatus(data.status);
  if (data.status !== undefined && statusValue === undefined) return null;
  if (data.title !== undefined && readText(data.title) === undefined) return null;
  if (data.role !== undefined && readText(data.role) === undefined) return null;
  if (data.child_agent_id !== undefined && readText(data.child_agent_id) === undefined) return null;
  if (data.child_session_id !== undefined && positiveId(data.child_session_id) === undefined) return null;
  return {
    title: readText(data.title),
    role: readText(data.role),
    childAgentId: readText(data.child_agent_id),
    childSessionId: positiveId(data.child_session_id),
    status: statusValue,
    writableRoot: readText(data.writable_root),
  };
}

export type ForkDisplay = { title: string; childSessionId: number; writableRoot?: string };

/** `fork-session-result`（`fork_session`）：分叉会话的标题与定位。 */
export function readForkDisplay(value: unknown): ForkDisplay | null {
  const data = record(value);
  if (!data || data.kind !== "fork-session-result") return null;
  const title = readText(data.title);
  const childSessionId = positiveId(data.child_session_id);
  if (title === undefined || childSessionId === undefined) return null;
  return { title, childSessionId, writableRoot: readText(data.writable_root) };
}

/** 读 `agent-session-result` 的白名单字段；对方 Run 的状态不在这里，读会话状态推送。 */
export function readAgentSessionResultDisplay(value: unknown): AgentSessionResultDisplay | null {
  const data = record(value);
  if (!data || data.kind !== "agent-session-result") return null;
  if (data.operation !== "send" && data.operation !== "status") return null;
  const sessionId = positiveId(data.session_id);
  if (sessionId === undefined) return null;
  if (data.agent_name !== null && data.agent_name !== undefined && readText(data.agent_name) === undefined) return null;
  if (data.final_output !== null && data.final_output !== undefined && typeof data.final_output !== "string") return null;
  if (data.end_reason !== null && data.end_reason !== undefined && typeof data.end_reason !== "string") return null;
  return {
    operation: data.operation,
    sessionId,
    agentName: readText(data.agent_name),
    finalOutput: typeof data.final_output === "string" ? data.final_output : undefined,
    endReason: typeof data.end_reason === "string" ? data.end_reason : undefined,
  };
}
