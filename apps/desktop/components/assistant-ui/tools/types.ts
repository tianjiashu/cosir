import type { TransportToolData, TransportToolPresentation, TransportToolStatus } from "@/lib/assistant/contract";

export type ToolArtifact = {
  backendStatus: TransportToolStatus;
  presentation: TransportToolPresentation;
  data: TransportToolData | null;
  error: string | null;
  errorCode: string | null;
};

export function readToolArtifact(value: unknown): ToolArtifact {
  if (typeof value !== "object" || value === null) {
    return { backendStatus: "unknown", presentation: {}, data: null, error: null, errorCode: null };
  }
  const candidate = value as Partial<ToolArtifact>;
  const status = candidate.backendStatus;
  const backendStatus: TransportToolStatus = status === "pending" || status === "running" || status === "completed" || status === "failed" || status === "cancelled"
    ? status
    : "unknown";
  const data = candidate.data ?? null;
  const dataRecord = asRecord(data);
  return {
    backendStatus,
    presentation: candidate.presentation ?? {},
    data,
    error: backendStatus === "cancelled"
      ? "已取消"
      : backendStatus === "failed"
        ? typeof dataRecord.status_hint === "string" ? dataRecord.status_hint : "执行失败"
        : null,
    errorCode: typeof candidate.errorCode === "string" ? candidate.errorCode : null,
  };
}

export function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : {};
}

/** Return an external URL only when it is safe to put in an anchor or image. */
export function safeExternalUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const candidate = value.trim();
  if (!candidate) return null;
  try {
    const url = new URL(candidate);
    return url.protocol === "http:" || url.protocol === "https:" ? candidate : null;
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
