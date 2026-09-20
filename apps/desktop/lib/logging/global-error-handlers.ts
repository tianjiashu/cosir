import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";

let installed = false;
const WINDOW_ERROR_REPORT_INTERVAL_MS = 1000;
let lastWindowErrorKey = "";
let lastWindowErrorAt = 0;
let suppressedWindowErrors = 0;

/** Register one process-wide, redacted sink for errors outside React boundaries. */
export function installGlobalFrontendErrorHandlers(target: Window = window): () => void {
  if (installed) return () => undefined;
  installed = true;
  lastWindowErrorKey = "";
  lastWindowErrorAt = 0;
  suppressedWindowErrors = 0;

  const onError = (event: ErrorEvent): void => {
    const message = event.message.trim().slice(0, 256);
    const key = `${message}|${event.filename}|${event.lineno}|${event.colno}`;
    const now = Date.now();
    if (key === lastWindowErrorKey && now - lastWindowErrorAt < WINDOW_ERROR_REPORT_INTERVAL_MS) {
      suppressedWindowErrors += 1;
      return;
    }
    const suppressed = suppressedWindowErrors;
    lastWindowErrorKey = key;
    lastWindowErrorAt = now;
    suppressedWindowErrors = 0;
    const traceId = newTraceId();
    void frontendLog("ERROR", "frontend_window_error", "前端窗口发生未捕获异常", {
      traceId,
      data: {
        message: message || "未提供异常消息",
        sourcePresent: event.filename.length > 0,
        line: event.lineno,
        column: event.colno,
        errorPresent: event.error !== null && event.error !== undefined,
        messagePresent: event.message.length > 0,
        suppressedSinceLastReport: suppressed,
      },
      error: event.error,
    }).catch(() => undefined);
  };

  const onUnhandledRejection = (event: PromiseRejectionEvent): void => {
    const traceId = newTraceId();
    void frontendLog("ERROR", "frontend_unhandled_rejection", "前端存在未处理的 Promise 异常", {
      traceId,
      data: {
        reasonPresent: event.reason !== null && event.reason !== undefined,
        reasonType: event.reason?.constructor?.name ?? typeof event.reason,
      },
      error: event.reason,
    }).catch(() => undefined);
  };

  target.addEventListener("error", onError);
  target.addEventListener("unhandledrejection", onUnhandledRejection);

  return () => {
    target.removeEventListener("error", onError);
    target.removeEventListener("unhandledrejection", onUnhandledRejection);
    installed = false;
    lastWindowErrorKey = "";
    lastWindowErrorAt = 0;
    suppressedWindowErrors = 0;
  };
}
