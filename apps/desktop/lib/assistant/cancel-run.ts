/** 向后端提交 Conversation Run 的显式取消命令。 */

import { requestRaw } from "@/lib/http/client";
import { createTimeoutAbort } from "@/lib/async/abort-timeout";
import { getActiveTraceId, newTraceId } from "@/lib/trace";

const CANCEL_REQUEST_TIMEOUT_MS = 15_000;

export type CancelRunResult =
  | { accepted: true }
  | { accepted: false; reason: "invalid_run_id" | "not_cancellable" | "rejected" | "network"; message: string };

export async function cancelRun(_taskId: number | null, runId: number): Promise<CancelRunResult> {
  if (!Number.isInteger(runId)) {
    return { accepted: false, reason: "invalid_run_id", message: "当前运行标识无效，无法取消。" };
  }
  const traceId = getActiveTraceId() ?? newTraceId();
  const { signal, clear } = createTimeoutAbort(CANCEL_REQUEST_TIMEOUT_MS);
  try {
    // 把 traceId 作为 init 字段交给 requestRaw 统一注入 X-Trace-Id 头；
    // 手写 header 会被 requestRaw 内部新生成的 id 覆盖，导致链路追踪关联失效。
    const response = await requestRaw(`/runs/${runId}/cancel`, {
      method: "POST",
      signal,
      traceId,
    });
    if (response.ok) return { accepted: true };
    if (response.status === 409) {
      const message = "后端当前不允许取消此运行，可能已经结束；请刷新对话状态后重试。";
      return { accepted: false, reason: "not_cancellable", message };
    }
    return { accepted: false, reason: "rejected", message: `取消请求被服务端拒绝（HTTP ${response.status}）。` };
  } catch {
    return { accepted: false, reason: "network", message: "取消请求失败，请检查本机后端连接。" };
  } finally {
    clear();
  }
}
