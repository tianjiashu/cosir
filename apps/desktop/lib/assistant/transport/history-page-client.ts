import { requestJson, type HttpRequestInit } from "@/lib/http/client";
import { parseHistoryPage } from "@/lib/assistant/transport/snapshot-validation";
import type { TransportHistoryPage } from "@/lib/assistant/contract";

/**
 * 读取完整历史中位置 `end` 之前的一页（往上翻时取），校验它与已加载消息的 id 不重复。
 * 位置已失效（窗口被完整快照替换）时后端回 409 `HISTORY_STALE`，以 HttpError 抛出。
 */
export async function requestHistoryPage(
  sessionId: number,
  end: number,
  loadedIds: ReadonlySet<string>,
  init: Pick<HttpRequestInit, "signal" | "traceId" | "timeoutMs"> = {},
): Promise<TransportHistoryPage> {
  const value = await requestJson<unknown>(`/sessions/${sessionId}/assistant/messages?end=${end}`, init);
  return parseHistoryPage(value, loadedIds);
}
