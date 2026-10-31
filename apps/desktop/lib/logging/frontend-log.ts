import type { FrontendLogEntry, FrontendLogLevel } from "@/electron/shared/host-contract";
import { desktopHost } from "@/lib/desktop-host";
import { newTraceId, type TraceId } from "@/lib/trace";

type FrontendLogWindow = Window & {
  __cosirFrontendLogs?: FrontendLogEntry[];
};

const CONSOLE_METHOD = { DEBUG: "debug", INFO: "info", WARNING: "warn", ERROR: "error" } as const satisfies Record<FrontendLogLevel, keyof Console>;

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

  // The desktop host is the production sink.  Keep a bounded, development-only
  // in-page sink as well so transport lifecycle tests can assert what the browser
  // observed without scraping console output or persisting another source of truth.
  if (import.meta.env.DEV && typeof window !== "undefined") {
    const logWindow = window as FrontendLogWindow;
    const logs = logWindow.__cosirFrontendLogs ?? (logWindow.__cosirFrontendLogs = []);
    logs.push(entry);
    if (logs.length > 500) logs.splice(0, logs.length - 500);

    // Keep the diagnostic trail visible in the renderer console while
    // preserving the bounded in-memory sink used by E2E diagnostics. Payloads
    // are already limited to lifecycle metadata; message/file contents never
    // enter this logger.
    const consoleData = { ...entry.data, ...(entry.error ? { error: entry.error } : {}) };
    console[CONSOLE_METHOD[level]](`[cosir.frontend] ${event}: ${msg}`, consoleData);
  }
  if (!desktopHost) {
    // 浏览器开发与 E2E 没有宿主日志，开发期已镜像到控制台，这里只补生产构建下的错误。
    if (!import.meta.env.DEV && level === "ERROR") console.error(`[${event}] ${msg}`, error);
    return;
  }
  try {
    await desktopHost.log.write(entry);
  } catch (cause) {
    // 宿主拒绝或写盘失败只影响诊断旁路，不能反过来打断调用方；留在控制台供开发者工具排查。
    console.warn("[cosir.frontend] frontend_log_write_failed", cause);
  }
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

function serializeFrontendError(value: unknown): NonNullable<FrontendLogEntry["error"]> {
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
