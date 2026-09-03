import { invoke } from "@tauri-apps/api/core";
import { newTraceId, type TraceId } from "@/lib/trace";

type FrontendLogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR";

export async function frontendLog(
  level: FrontendLogLevel,
  event: string,
  msg: string,
  options: { traceId?: TraceId; data?: Record<string, unknown>; error?: unknown } = {},
): Promise<void> {
  const error = options.error instanceof Error
    ? { type: options.error.name, message: options.error.message, stack: options.error.stack }
    : options.error === undefined ? null : { message: String(options.error) };
  const entry = {
    ts: new Date().toISOString(),
    level,
    logger: "coding_agent.frontend",
    trace_id: options.traceId ?? newTraceId(),
    caller: "desktop.frontend",
    event,
    msg,
    data: options.data ?? {},
    error,
    truncated: false,
  };
  try {
    await invoke("write_frontend_log", { entry });
  } catch {
    // 浏览器开发模式没有 Tauri command，保留控制台作为开发期兜底。
    if (level === "ERROR") console.error(`[${event}] ${msg}`, options.error);
  }
}
