/** 向后端提交停止 Task 当前执行的命令。
 *
 * 按 taskId 取消：同一 Task 内 Run 串行，后端命中的永远是当前执行，不依赖 snapshot 里可能滞后的
 * run_id。接受只表示信号已登记，Run 终态仍以 canonical snapshot 为准；重复请求幂等成功。
 */

import { requestRaw } from "@/lib/http/client";
import { createTimeoutAbort } from "@/lib/async/abort-timeout";
import { newTraceId } from "@/lib/trace";

const CANCEL_REQUEST_TIMEOUT_MS = 15_000;

export type CancelTaskResult =
  | { accepted: true }
  | { accepted: false; reason: "invalid_task_id" | "not_cancellable" | "rejected" | "network"; message: string };

export async function cancelTask(taskId: number | null): Promise<CancelTaskResult> {
  if (taskId === null || !Number.isInteger(taskId)) {
    return { accepted: false, reason: "invalid_task_id", message: "当前任务标识无效，无法停止。" };
  }
  const traceId = newTraceId();
  const { signal, clear } = createTimeoutAbort(CANCEL_REQUEST_TIMEOUT_MS);
  try {
    // 把 traceId 作为 init 字段交给 requestRaw 统一注入 X-Trace-Id 头；
    // 手写 header 会被 requestRaw 内部新生成的 id 覆盖，导致链路追踪关联失效。
    const response = await requestRaw(`/tasks/${taskId}/cancel`, {
      method: "POST",
      signal,
      traceId,
    });
    if (response.ok) return { accepted: true };
    if (response.status === 409) {
      const message = "当前没有可停止的运行，可能已经结束；请刷新对话状态后重试。";
      return { accepted: false, reason: "not_cancellable", message };
    }
    return { accepted: false, reason: "rejected", message: `停止请求被服务端拒绝（HTTP ${response.status}）。` };
  } catch {
    return { accepted: false, reason: "network", message: "停止请求失败，请检查本机后端连接。" };
  } finally {
    clear();
  }
}
