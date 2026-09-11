import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";

let installed = false;

/** Register one process-wide, redacted sink for errors outside React boundaries. */
export function installGlobalFrontendErrorHandlers(target: Window = window): () => void {
  if (installed) return () => undefined;
  installed = true;

  const onError = (event: ErrorEvent): void => {
    const traceId = newTraceId();
    void frontendLog("ERROR", "frontend_window_error", "前端窗口发生未捕获异常", {
      traceId,
      data: {
        sourcePresent: event.filename.length > 0,
        line: event.lineno,
        column: event.colno,
        errorPresent: event.error !== null && event.error !== undefined,
        messagePresent: event.message.length > 0,
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
  };
}
