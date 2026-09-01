/** 向后端提交 Conversation Run 的显式取消命令。 */

import { getApiBaseUrl } from "@/lib/api/client";

export async function cancelRun(taskId: number | null, runId: number): Promise<boolean> {
  if (!Number.isInteger(runId)) {
    console.error("[cancelRun] 跳过取消：runId 非法", { taskId, runId });
    return false;
  }
  try {
    const response = await fetch(`${getApiBaseUrl()}/runs/${runId}/cancel`, {
      method: "POST",
      headers: { Accept: "application/json" },
    });
    if (response.ok || response.status === 409) return true;
    console.error("[cancelRun] 取消请求被服务端拒绝", {
      taskId,
      runId,
      status: response.status,
      body: await response.text(),
    });
    return false;
  } catch (error) {
    console.error("[cancelRun] 取消请求失败", {
      taskId,
      runId,
      error: error instanceof Error ? error.message : String(error),
    });
    return false;
  }
}
