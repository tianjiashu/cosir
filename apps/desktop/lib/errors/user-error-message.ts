import { UserFacingError } from "@/lib/errors/user-facing-error";
import { HttpError } from "@/lib/http/errors";

/**
 * 把任意错误转换为可以展示给用户的中文文案——界面展示错误的唯一入口。
 *
 * 不要在界面直接显示 `error.message`（会透出库与网络层的英文原文）。规则按
 * 「用户能否据此纠正」排列：
 * 1. UserFacingError：前端主动写给用户的文案，原样展示。
 * 2. HttpError：后端给出可读原因（结构化错误或业务 detail）时展示原因；
 *    框架默认的英文状态短语（如 FastAPI 的 "Not Found"）按状态码换成中文。
 * 3. 超时 / 取消 / 网络不可达：固定文案（附件失效等前端错误已是 UserFacingError）。
 * 4. 其余一律使用调用方的兜底文案，不透出库或运行时的原始英文与堆栈。
 * 不复述响应体原文（AGENTS.md 错误通道约束）；诊断细节由调用方写入结构化日志。
 */
export function userErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof UserFacingError) return error.message;
  if (error instanceof HttpError) return httpMessage(error) ?? fallback;
  if (!(error instanceof Error)) return fallback;
  switch (error.name) {
    case "TimeoutError":
      return "请求超时，请检查本机后端是否仍在运行";
    case "AbortError":
      return "请求已取消";
    default:
      return isNetworkFailure(error) ? "无法连接本机后端，请确认后端正在运行" : fallback;
  }
}

const FRAMEWORK_STATUS_PHRASES = new Set([
  "Bad Request",
  "Unauthorized",
  "Forbidden",
  "Not Found",
  "Method Not Allowed",
  "Conflict",
  "Unprocessable Entity",
  "Unprocessable Content",
  "Too Many Requests",
  "Internal Server Error",
  "Bad Gateway",
  "Service Unavailable",
]);

function httpMessage(error: HttpError): string | null {
  const message = error.message.trim();
  const generic = !message
    || FRAMEWORK_STATUS_PHRASES.has(message)
    || message.startsWith("HTTP request failed with status");
  if (!generic) return message;
  if (error.status === 404) return "请求的内容不存在或已被删除";
  if (error.status === 409) return "当前状态不允许该操作，请刷新后重试";
  if (error.status === 422 || error.status === 400) return "提交的内容不符合要求，请检查后重试";
  if (error.status >= 500) return "本机后端处理失败，请查看后端日志";
  return null;
}

/** fetch 在连接失败时抛 TypeError，各 WebView 的文案不同（Failed to fetch / Load failed / NetworkError…）。 */
function isNetworkFailure(error: Error): boolean {
  // 只认 fetch 的网络失败文案；其余 TypeError 是程序缺陷，必须走兜底文案而不是误报「连不上后端」。
  return (error.name === "TypeError" || error.name === "NetworkError")
    && /failed to fetch|load failed|networkerror|network request failed/i.test(error.message);
}
