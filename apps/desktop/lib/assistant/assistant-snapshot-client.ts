import { requestJson, type HttpRequestInit } from "@/lib/http/client";
import { parseHistoryPage, parseTransportState } from "@/lib/assistant/snapshot-validation";
import type { TransportHistoryPage, TransportState } from "@/lib/assistant/contract";

/**
 * 读取并校验任务的 canonical Assistant Transport snapshot。
 *
 * 该模块只拥有 snapshot endpoint 的地址拼接和 wire-schema 校验。会话内的读取
 * 统一经 `canonical-snapshot.ts` 的 `fetchCanonicalSnapshot`（超时、取消、后端代守卫），
 * 不要直接调用本函数；重试与结果如何投影到 runtime 仍由各自的恢复用例负责。
 */
export async function requestAssistantSnapshot(
  taskId: number,
  init: Pick<HttpRequestInit, "signal" | "traceId" | "timeoutMs"> = {},
): Promise<TransportState> {
  const value = await requestJson<unknown>(`/tasks/${taskId}/assistant/state`, init);
  return parseTransportState(value);
}

/**
 * 读取完整历史中位置 `end` 之前的一页（往上翻时取），校验它与已加载消息的 id 不重复。
 * 位置已失效（窗口被完整快照替换）时后端回 409 `HISTORY_STALE`，以 HttpError 抛出。
 */
export async function requestHistoryPage(
  taskId: number,
  end: number,
  loadedIds: ReadonlySet<string>,
  init: Pick<HttpRequestInit, "signal" | "traceId"> = {},
): Promise<TransportHistoryPage> {
  const value = await requestJson<unknown>(`/tasks/${taskId}/assistant/messages?end=${end}`, init);
  return parseHistoryPage(value, loadedIds);
}
