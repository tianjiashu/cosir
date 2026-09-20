import type { TransportToolDisplayData, TransportToolPresentation, TransportToolStatus } from "@/lib/assistant/contract";

export type ToolArtifact = {
  backendStatus: TransportToolStatus;
  presentation: TransportToolPresentation;
  display_data: TransportToolDisplayData | null;
  error: string | null;
  errorCode: string | null;
  child_task_id?: number;
  child_run_id?: number;
  agent_role?: string;
  delegation_ref_seq?: number;
  terminal_output_seq?: number;
};

export function readToolArtifact(value: unknown): ToolArtifact {
  if (typeof value !== "object" || value === null) {
    return { backendStatus: "unknown", presentation: {}, display_data: null, error: null, errorCode: null };
  }
  const candidate = value as Partial<ToolArtifact>;
  const status = candidate.backendStatus;
  const backendStatus: TransportToolStatus = status === "pending" || status === "running" || status === "completed" || status === "failed" || status === "cancelled"
    ? status
    : "unknown";
  const display_data = candidate.display_data ?? null;
  const displayDataRecord = asRecord(display_data);
  return {
    backendStatus,
    presentation: candidate.presentation ?? {},
    display_data,
    error: backendStatus === "cancelled"
      ? "已取消"
      : backendStatus === "failed"
        ? typeof displayDataRecord.status_hint === "string" ? displayDataRecord.status_hint : "执行失败"
        : null,
    errorCode: typeof candidate.errorCode === "string" ? candidate.errorCode : null,
    child_task_id: typeof candidate.child_task_id === "number" && Number.isInteger(candidate.child_task_id) && candidate.child_task_id > 0 ? candidate.child_task_id : undefined,
    child_run_id: typeof candidate.child_run_id === "number" && Number.isInteger(candidate.child_run_id) && candidate.child_run_id > 0 ? candidate.child_run_id : undefined,
    agent_role: typeof candidate.agent_role === "string" && candidate.agent_role.trim() ? candidate.agent_role : undefined,
    delegation_ref_seq: typeof candidate.delegation_ref_seq === "number" && Number.isInteger(candidate.delegation_ref_seq) && candidate.delegation_ref_seq >= 0 ? candidate.delegation_ref_seq : undefined,
    terminal_output_seq: typeof candidate.terminal_output_seq === "number" && Number.isInteger(candidate.terminal_output_seq) && candidate.terminal_output_seq >= 0 ? candidate.terminal_output_seq : undefined,
  };
}

export function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : {};
}

function isPrivateIpv4(hostname: string): boolean {
  const octets = hostname.split(".").map(Number);
  if (octets.length !== 4 || octets.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return false;
  const [first, second] = octets;
  return first === 0 || first === 10 || first === 127 || (first === 169 && second === 254)
    || (first === 172 && second >= 16 && second <= 31)
    || (first === 192 && second === 168)
    || (first === 100 && second >= 64 && second <= 127);
}

/** Return an external URL only when it is safe to put in an anchor or image. */
export function safeExternalUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const candidate = value.trim();
  if (!candidate) return null;
  try {
    const url = new URL(candidate);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    const hostname = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    if (url.username || url.password || hostname === "localhost" || hostname.endsWith(".localhost")) return null;
    if (isPrivateIpv4(hostname) || hostname === "::1" || hostname.startsWith("fc") || hostname.startsWith("fd") || hostname.startsWith("fe80:")) return null;
    return candidate;
  } catch {
    return null;
  }
}

export function displayValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
