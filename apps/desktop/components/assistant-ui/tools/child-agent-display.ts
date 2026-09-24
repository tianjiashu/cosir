import type { TransportToolStatus } from "@/lib/assistant/contract";

export type ChildAgentLifecycleStatus = Extract<TransportToolStatus, "pending" | "running" | "completed" | "failed" | "cancelled">;

export type DelegationDisplay = {
  title?: string;
  role?: string;
  childAgentId?: string;
  childTaskId?: number;
  childRunId?: number;
  status?: ChildAgentLifecycleStatus;
};

export type ChildAgentWaitMessage = {
  childTaskId: number;
  childRunId: number;
  status: Extract<ChildAgentLifecycleStatus, "completed" | "failed" | "cancelled">;
  finalOutput?: string;
  endReason?: string;
};

export type ChildAgentResultDisplay = {
  operation: "send" | "status";
  childTaskId: number;
  childRunId: number;
  status: ChildAgentLifecycleStatus;
  agentId?: string;
  agentName?: string;
  finalOutput?: string;
  endReason?: string;
};

export type ChildAgentWaitDisplay = {
  timedOut: boolean;
  messages: ChildAgentWaitMessage[];
  pending: Array<{ childTaskId: number; status?: ChildAgentLifecycleStatus }>;
  interruptedBy: "parent_cancelled" | "session_closed" | "shutdown" | null;
};

const CHILD_AGENT_STATUSES = new Set<ChildAgentLifecycleStatus>([
  "pending",
  "running",
  "completed",
  "failed",
  "cancelled",
]);

const INTERRUPTIONS = new Set<NonNullable<ChildAgentWaitDisplay["interruptedBy"]>>([
  "parent_cancelled",
  "session_closed",
  "shutdown",
]);

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

export function positiveId(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : undefined;
}

export function childAgentStatus(value: unknown): ChildAgentLifecycleStatus | undefined {
  return typeof value === "string" && CHILD_AGENT_STATUSES.has(value as ChildAgentLifecycleStatus)
    ? value as ChildAgentLifecycleStatus
    : undefined;
}

/** Read the allowlisted delegation projection; prompt/error-shaped fields are ignored. */
export function readDelegationDisplay(value: unknown): DelegationDisplay | null {
  const data = record(value);
  if (!data || data.kind !== "delegation-result") return null;
  const statusValue = data.status === undefined ? undefined : childAgentStatus(data.status);
  if (data.status !== undefined && statusValue === undefined) return null;
  if (data.title !== undefined && nonEmptyString(data.title) === undefined) return null;
  if (data.role !== undefined && nonEmptyString(data.role) === undefined) return null;
  if (data.child_agent_id !== undefined && nonEmptyString(data.child_agent_id) === undefined) return null;
  if (data.child_task_id !== undefined && positiveId(data.child_task_id) === undefined) return null;
  if (data.child_run_id !== undefined && positiveId(data.child_run_id) === undefined) return null;
  return {
    title: nonEmptyString(data.title),
    role: nonEmptyString(data.role),
    childAgentId: nonEmptyString(data.child_agent_id),
    childTaskId: positiveId(data.child_task_id),
    childRunId: positiveId(data.child_run_id),
    status: statusValue,
  };
}

/** Read the allowlisted result projection for child_agent_send/status. */
export function readChildAgentResultDisplay(value: unknown): ChildAgentResultDisplay | null {
  const data = record(value);
  if (!data || data.kind !== "child-agent-result") return null;
  if (data.operation !== "send" && data.operation !== "status") return null;
  const status = childAgentStatus(data.status);
  if (!status) return null;
  const childTaskId = positiveId(data.child_task_id);
  const childRunId = positiveId(data.child_run_id);
  if (childTaskId === undefined || childRunId === undefined) return null;
  if (data.agent_id !== null && data.agent_id !== undefined && nonEmptyString(data.agent_id) === undefined) return null;
  if (data.agent_name !== null && data.agent_name !== undefined && nonEmptyString(data.agent_name) === undefined) return null;
  if (data.final_output !== null && data.final_output !== undefined && typeof data.final_output !== "string") return null;
  if (data.end_reason !== null && data.end_reason !== undefined && typeof data.end_reason !== "string") return null;
  return {
    operation: data.operation,
    childTaskId,
    childRunId,
    status,
    agentId: nonEmptyString(data.agent_id),
    agentName: nonEmptyString(data.agent_name),
    finalOutput: typeof data.final_output === "string" ? data.final_output : undefined,
    endReason: typeof data.end_reason === "string" ? data.end_reason : undefined,
  };
}

/** Strictly parse the generic child-agent wait result before rendering it. */
export function readChildAgentWaitDisplay(value: unknown): ChildAgentWaitDisplay | null {
  const data = record(value);
  if (!data || data.kind !== "child-agent-wait-result" || typeof data.timed_out !== "boolean") return null;
  if (!Array.isArray(data.messages) || !Array.isArray(data.pending)) return null;
  const interruptedBy = data.interrupted_by === null
    ? null
    : INTERRUPTIONS.has(data.interrupted_by as NonNullable<ChildAgentWaitDisplay["interruptedBy"]>)
      ? data.interrupted_by as NonNullable<ChildAgentWaitDisplay["interruptedBy"]>
      : undefined;
  if (interruptedBy === undefined) return null;

  const messages: ChildAgentWaitMessage[] = [];
  for (const raw of data.messages) {
    const message = record(raw);
    const status = message ? message.status : undefined;
    if (!message || positiveId(message.child_task_id) === undefined || positiveId(message.child_run_id) === undefined) return null;
    if (status !== "completed" && status !== "failed" && status !== "cancelled") return null;
    if (message.final_output !== undefined && message.final_output !== null && typeof message.final_output !== "string") return null;
    if (message.end_reason !== undefined && message.end_reason !== null && typeof message.end_reason !== "string") return null;
    messages.push({
      childTaskId: positiveId(message.child_task_id)!,
      childRunId: positiveId(message.child_run_id)!,
      status,
      finalOutput: typeof message.final_output === "string" ? message.final_output : undefined,
      endReason: typeof message.end_reason === "string" ? message.end_reason : undefined,
    });
  }

  const pending: ChildAgentWaitDisplay["pending"] = [];
  for (const raw of data.pending) {
    const item = record(raw);
    if (!item || positiveId(item.child_task_id) === undefined) return null;
    const status = item.status === undefined || item.status === null ? undefined : childAgentStatus(item.status);
    if (item.status !== undefined && item.status !== null && status === undefined) return null;
    pending.push({ childTaskId: positiveId(item.child_task_id)!, status });
  }
  return { timedOut: data.timed_out, messages, pending, interruptedBy };
}

export function childAgentStatusLabel(status: ChildAgentLifecycleStatus): string {
  switch (status) {
    case "pending": return "准备执行";
    case "running": return "运行中";
    case "completed": return "已完成";
    case "failed": return "失败";
    case "cancelled": return "已取消";
  }
}

export function interruptionLabel(value: NonNullable<ChildAgentWaitDisplay["interruptedBy"]>): string {
  switch (value) {
    case "parent_cancelled": return "父运行已取消";
    case "session_closed": return "子会话已关闭";
    case "shutdown": return "后端已关闭";
  }
}
