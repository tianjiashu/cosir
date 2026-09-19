import { requestJson, type HttpRequestInit } from "@/lib/http/client";
import { parseTransportState } from "@/lib/assistant/snapshot-validation";
import type { TransportState } from "@/lib/assistant/contract";

/**
 * 读取并校验任务的 canonical Assistant Transport snapshot。
 *
 * 该模块只拥有 snapshot endpoint 的地址拼接和 wire-schema 校验；重试、
 * 取消、generation 以及结果如何投影到 runtime，仍由各自的生命周期用例负责。
 */
export async function requestAssistantSnapshot(
  taskId: number,
  init: Pick<HttpRequestInit, "signal" | "traceId"> = {},
): Promise<TransportState> {
  const value = await requestJson<unknown>(`/tasks/${taskId}/assistant/state`, init);
  return parseTransportState(value);
}
