import type { TransportToolStatus } from "@/lib/assistant/contract";

export type ChildAgentLifecycleStatus = Extract<TransportToolStatus, "pending" | "running" | "completed" | "failed" | "cancelled">;

export type DelegationDisplay = {
  title?: string;
  role?: string;
  childAgentId?: string;
  childTaskId?: number;
  status?: ChildAgentLifecycleStatus;
};

export type ChildAgentResultDisplay = {
  operation: "send" | "status";
  childTaskId: number;
  status: ChildAgentLifecycleStatus;
  agentId?: string;
  agentName?: string;
  finalOutput?: string;
  endReason?: string;
};

const CHILD_AGENT_STATUSES = new Set<ChildAgentLifecycleStatus>([
  "pending",
  "running",
  "completed",
  "failed",
  "cancelled",
]);

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function positiveId(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : undefined;
}

function childAgentStatus(value: unknown): ChildAgentLifecycleStatus | undefined {
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
  return {
    title: nonEmptyString(data.title),
    role: nonEmptyString(data.role),
    childAgentId: nonEmptyString(data.child_agent_id),
    childTaskId: positiveId(data.child_task_id),
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
  if (childTaskId === undefined) return null;
  if (data.agent_id !== null && data.agent_id !== undefined && nonEmptyString(data.agent_id) === undefined) return null;
  if (data.agent_name !== null && data.agent_name !== undefined && nonEmptyString(data.agent_name) === undefined) return null;
  if (data.final_output !== null && data.final_output !== undefined && typeof data.final_output !== "string") return null;
  if (data.end_reason !== null && data.end_reason !== undefined && typeof data.end_reason !== "string") return null;
  return {
    operation: data.operation,
    childTaskId,
    status,
    agentId: nonEmptyString(data.agent_id),
    agentName: nonEmptyString(data.agent_name),
    finalOutput: typeof data.final_output === "string" ? data.final_output : undefined,
    endReason: typeof data.end_reason === "string" ? data.end_reason : undefined,
  };
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

