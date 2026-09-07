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
  return {
    backendStatus,
    presentation: candidate.presentation ?? {},
    data: candidate.data ?? null,
    error: typeof candidate.error === "string" ? candidate.error : null,
    errorCode: typeof candidate.errorCode === "string" ? candidate.errorCode : null,
  };
}

export function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : {};
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
