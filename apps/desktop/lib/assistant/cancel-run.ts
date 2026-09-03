/** 向后端提交 Conversation Run 的显式取消命令。 */

import { getApiBaseUrl } from "@/lib/api/client";
import { getActiveTraceId, newTraceId } from "@/lib/trace";
import { frontendLog } from "@/lib/logging/frontend-log";

export async function cancelRun(taskId: number | null, runId: number): Promise<boolean> {
  if (!Number.isInteger(runId)) {
    await frontendLog("WARNING", "cancel_run_invalid_id", "取消请求跳过：runId 非法", { data: { taskId, runId } });
    return false;
  }
  const traceId = getActiveTraceId() ?? newTraceId();
  try {
    const response = await fetch(`${getApiBaseUrl()}/runs/${runId}/cancel`, {
      method: "POST",
      headers: { Accept: "application/json", "X-Trace-Id": traceId },
    });
    if (response.ok || response.status === 409) return true;
    await frontendLog("ERROR", "cancel_run_rejected", "取消请求被服务端拒绝", {
      traceId,
      data: { taskId, runId, status: response.status },
    });
    return false;
  } catch (error) {
    await frontendLog("ERROR", "cancel_run_failed", "取消请求失败", {
      traceId,
      data: { taskId, runId },
      error,
    });
    return false;
  }
}
