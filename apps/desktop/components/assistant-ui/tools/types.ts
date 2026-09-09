import type {
  TransportToolData,
  TransportToolPresentation,
  TransportToolStatus,
  WebExtractStatusData,
  WebSearchResultData,
} from "@/lib/assistant/contract";

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

export function isWebSearchData(value: TransportToolData | null): value is WebSearchResultData {
  return value?.kind === "web-search-results" && Array.isArray(value.results);
}

export function isWebExtractStatusData(value: TransportToolData | null): value is WebExtractStatusData {
  return value?.kind === "web-extract-status" && Array.isArray(value.sites);
}

export type WebExtractDisplayStatus = WebExtractStatusData["sites"][number]["status"] | "cancelled";

export function resolveWebExtractSiteStatus(
  siteStatus: WebExtractStatusData["sites"][number]["status"],
  backendStatus: TransportToolStatus,
): WebExtractDisplayStatus {
  if (siteStatus !== "pending" && siteStatus !== "running") return siteStatus;
  if (backendStatus === "running") return "running";
  if (backendStatus === "cancelled") return "cancelled";
  if (backendStatus === "failed") return "failed";
  return siteStatus;
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
