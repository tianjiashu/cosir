import type { FrontendLogEntry, FrontendLogLevel } from "@/electron/shared/host-contract";
import { desktopHost } from "@/lib/desktop-host";
import { newTraceId, type TraceId } from "@/lib/trace";

type FrontendLogWindow = Window & {
  __cosirFrontendLogs?: FrontendLogEntry[];
};

const CONSOLE_METHOD = { DEBUG: "debug", INFO: "info", WARNING: "warn", ERROR: "error" } as const satisfies Record<FrontendLogLevel, keyof Console>;

/**
 * 前端结构化日志的唯一入口。
 *
 * 契约：永不 reject、不同步抛错。调用方因此可以 `void frontendLog(...)` 或在错误路径上 `await`，
 * 都不需要另外 `.catch`；失败（异常值无法序列化、宿主拒绝、写盘失败）只在控制台留一条警告。
 */
export async function frontendLog(
  level: FrontendLogLevel,
  event: string,
  msg: string,
  options: { traceId?: TraceId; data?: Record<string, unknown>; error?: unknown } = {},
): Promise<void> {
  try {
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
    await desktopHost.log.write(entry);
  } catch (cause) {
    // 日志是诊断旁路：构造、开发期镜像或宿主写盘任一步失败都只落到控制台，不能反过来打断调用方。
    console.warn("[cosir.frontend] frontend_log_failed", cause);
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

/** 任意抛出值都能得到一条错误描述：值本身无法转成文本（如 `Object.create(null)`）时降级为占位，不丢整条日志。 */
function serializeFrontendError(value: unknown): NonNullable<FrontendLogEntry["error"]> {
  try {
    return describeFrontendError(value);
  } catch {
    return { message: "异常值无法转成文本", truncated: false };
  }
}

function describeFrontendError(value: unknown): NonNullable<FrontendLogEntry["error"]> {
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
