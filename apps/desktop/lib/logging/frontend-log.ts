import { invoke } from "@tauri-apps/api/core";
import { newTraceId, type TraceId } from "@/lib/trace";

type FrontendLogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR";

type FrontendLogEntry = {
  ts: string;
  level: FrontendLogLevel;
  logger: string;
  trace_id: TraceId;
  caller: string;
  event: string;
  msg: string;
  data: Record<string, unknown>;
  error: { type?: string; message: string; stack?: string; truncated: boolean } | null;
  truncated: boolean;
};

type FrontendLogWindow = Window & {
  __cosirFrontendLogs?: FrontendLogEntry[];
};

export async function frontendLog(
  level: FrontendLogLevel,
  event: string,
  msg: string,
  options: { traceId?: TraceId; data?: Record<string, unknown>; error?: unknown } = {},
): Promise<void> {
  const error = options.error === undefined ? null : serializeFrontendError(options.error);
  const entry: FrontendLogEntry = {
    ts: new Date().toISOString(),
    level,
    logger: "coding_agent.frontend",
    trace_id: options.traceId ?? newTraceId(),
    caller: "desktop.frontend",
    event,
    msg,
    data: options.data ?? {},
    error,
    truncated: error?.truncated ?? false,
  };

  // The Tauri command is the production sink.  Keep a bounded, development-only
  // in-page sink as well so transport lifecycle tests can assert what the browser
  // observed without scraping console output or persisting another source of truth.
  if (import.meta.env.DEV && typeof window !== "undefined") {
    const logWindow = window as FrontendLogWindow;
    const logs = logWindow.__cosirFrontendLogs ?? (logWindow.__cosirFrontendLogs = []);
    logs.push(entry);
    if (logs.length > 500) logs.splice(0, logs.length - 500);

    // Keep the diagnostic trail visible in the Tauri WebView console while
    // preserving the bounded in-memory sink used by E2E diagnostics. Payloads
    // are already limited to lifecycle metadata; message/file contents never
    // enter this logger.
    const consoleData = { ...entry.data, ...(entry.error ? { error: entry.error } : {}) };
    if (level === "ERROR") console.error(`[cosir.frontend] ${event}: ${msg}`, consoleData);
    else if (level === "WARNING") console.warn(`[cosir.frontend] ${event}: ${msg}`, consoleData);
    else if (level === "DEBUG") console.debug(`[cosir.frontend] ${event}: ${msg}`, consoleData);
    else console.info(`[cosir.frontend] ${event}: ${msg}`, consoleData);
  }
  try {
    await invoke("write_frontend_log", { entry });
  } catch {
    // 浏览器开发模式没有 Tauri command，保留控制台作为开发期兜底。
    if (level === "ERROR") console.error(`[${event}] ${msg}`, error);
  }
}

/** 返回不会复述服务端响应体的固定 UI 错误文案。 */
export function safeFrontendErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.name === "AbortError") return "请求已取消";
  if (error instanceof Error && error.name === "LocalAttachmentUnavailableError") {
    return "附件已失效，请重新选择附件";
  }
  return fallback;
}

const MAX_ERROR_MESSAGE_LENGTH = 512;
const MAX_ERROR_STACK_LENGTH = 4096;

function redactDiagnosticText(value: string, maxLength: number): { value: string; truncated: boolean } {
  const redacted = value
    .replace(/(authorization|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|password|secret)\s*[:=]\s*("[^"]*"|'[^']*'|[^\s,;}]+)/gi, "$1=[REDACTED]")
    .replace(/\bBearer\s+[^\s]+/gi, "Bearer [REDACTED]")
    .replace(/\b(?:sk|key)-[A-Za-z0-9_-]{12,}\b/g, "[REDACTED]");
  return {
    value: redacted.slice(0, maxLength),
    truncated: redacted.length > maxLength,
  };
}

function serializeFrontendError(value: unknown): { type?: string; message: string; stack?: string; truncated: boolean } {
  const rawMessage = value instanceof Error ? value.message : String(value);
  const message = redactDiagnosticText(rawMessage, MAX_ERROR_MESSAGE_LENGTH);
  const rawStack = value instanceof Error && typeof value.stack === "string" ? value.stack : "";
  const stack = redactDiagnosticText(rawStack, MAX_ERROR_STACK_LENGTH);
  return {
    ...(value instanceof Error ? { type: value.name } : {}),
    message: message.value || "未提供异常消息",
    ...(stack.value ? { stack: stack.value } : {}),
    truncated: message.truncated || stack.truncated,
  };
}
