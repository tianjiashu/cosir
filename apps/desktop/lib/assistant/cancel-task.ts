/** 向后端提交停止 Task 当前执行的命令。
 *
 * 按 taskId 取消：同一 Task 内 Run 串行，后端命中的永远是当前执行。命令只投递（202）：运行中
 * 的执行在后端入口同步打取消信号，挂起的 Run 由调度 actor 直接迁移；是否真的停下、停成什么状态
 * 一律以 Task 长订阅里的 `activity` 为准，这里不判断「有没有可停止的对象」。
 */

import { requestRaw } from "@/lib/http/client";
import { createTimeoutAbort } from "@/lib/async/abort-timeout";
import { newTraceId } from "@/lib/trace";

const CANCEL_REQUEST_TIMEOUT_MS = 15_000;

export type CancelTaskResult =
  | { accepted: true }
  | { accepted: false; reason: "invalid_task_id" | "rejected" | "network"; message: string };

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
    return { accepted: false, reason: "rejected", message: `停止请求被服务端拒绝（HTTP ${response.status}）。` };
  } catch {
    return { accepted: false, reason: "network", message: "停止请求失败，请检查本机后端连接。" };
  } finally {
    clear();
  }
}
