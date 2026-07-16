/**
 * 客户端到后端 trace 传导工具。
 *
 * 负责生成一次前端用户操作的 `trace_id`，并把它注入 HTTP/SSE 请求头。
 * 业务 service 只调用这里，不直接拼 trace header。
 *
 * @module services/tracePropagation
 */

import type {
  BackendTraceHeaders,
  ClientTraceContext,
  TraceHeaders,
} from "@shared/tracePropagation";
import { useClientTraceStore } from "@/stores/clientTraceStore";

/**
 * 创建新的客户端 trace 上下文。
 *
 * @param fields - 可选 task/run 关联字段。
 * @returns 新的客户端 trace 上下文。
 */
export function createClientTrace(
  fields: Partial<Omit<ClientTraceContext, "traceId" | "startedAt">> = {},
): ClientTraceContext {
  return {
    traceId: newTraceId(),
    startedAt: new Date().toISOString(),
    ...fields,
  };
}

/**
 * 获取当前客户端 trace；没有时会创建并写入 store。
 *
 * @param fields - 可选 task/run 关联字段。
 * @returns 当前或新建的客户端 trace 上下文。
 *
 * @sideeffect 当前没有 trace 上下文时写入 clientTraceStore。
 */
export function ensureClientTrace(
  fields: Partial<Omit<ClientTraceContext, "traceId" | "startedAt">> = {},
): ClientTraceContext {
  const store = useClientTraceStore.getState();
  const current = store.currentTrace;
  if (current) {
    const additions = withoutEmpty(fields);
    if (Object.keys(additions).length === 0) {
      return current;
    }
    const merged = { ...current, ...additions };
    if (hasTraceChange(current, merged)) {
      store.setCurrentTrace(merged);
      return merged;
    }
    return current;
  }
  const trace = createClientTrace(fields);
  store.setCurrentTrace(trace);
  return trace;
}

/**
 * 开始一次新的前端用户操作 trace。
 *
 * 每次明确用户动作入口应调用该函数，避免多个不相关动作复用同一个
 * `trace_id`。
 *
 * @param fields - 可选 task/run 关联字段。
 * @returns 新创建并写入 store 的客户端 trace 上下文。
 *
 * @sideeffect 覆盖 clientTraceStore.currentTrace。
 */
export function beginClientTrace(
  fields: Partial<Omit<ClientTraceContext, "traceId" | "startedAt">> = {},
): ClientTraceContext {
  const trace = createClientTrace(fields);
  useClientTraceStore.getState().setCurrentTrace(trace);
  return trace;
}

/**
 * 结束当前前端用户操作 trace。
 *
 * @sideeffect 清空 clientTraceStore.currentTrace。
 */
export function endClientTrace(): void {
  useClientTraceStore.getState().clearCurrentTrace();
}

/**
 * 判断当前是否已有活跃前端用户操作 trace。
 *
 * @returns 存在 currentTrace 时返回 true。
 */
export function hasClientTrace(): boolean {
  return Boolean(useClientTraceStore.getState().currentTrace);
}

/**
 * 构造一次 HTTP/SSE 请求需要携带的 trace headers。
 *
 * @param fields - 可选 task/run 关联字段。
 * @returns 请求 trace header 和当前 trace 上下文。
 *
 * @sideeffect 没有 currentTrace 时创建并写入 clientTraceStore。
 */
export function buildTraceHeaders(
  fields: Partial<Omit<ClientTraceContext, "traceId" | "startedAt">> = {},
): {
  headers: TraceHeaders;
  trace: ClientTraceContext;
} {
  const trace = ensureClientTrace(fields);
  return {
    headers: {
      "x-trace-id": trace.traceId,
    },
    trace,
  };
}

/**
 * 从响应 header 读取后端确认的 trace 信息。
 *
 * @param response - fetch Response 对象。
 * @param fallbackTraceId - 请求发出时使用的 trace ID。
 * @returns 后端确认后的 trace 信息。
 */
export function readBackendTraceHeaders(response: Response, fallbackTraceId: string): BackendTraceHeaders {
  const headers = response.headers;
  return {
    traceId: headers?.get?.("x-trace-id") || fallbackTraceId,
  };
}

/**
 * 记录一次后端响应确认的 trace。
 *
 * @param headers - 后端确认的 trace header。
 *
 * @sideeffect 写入 clientTraceStore 的 lastTrace。
 */
export function recordBackendTrace(headers: BackendTraceHeaders): void {
  useClientTraceStore.getState().recordBackendTrace(headers);
}

/**
 * 生成新的 trace ID。
 *
 * @returns 32 位小写十六进制 trace ID。
 */
export function newTraceId(): string {
  return randomHex(16);
}

/**
 * 生成指定字节数对应的小写十六进制字符串。
 *
 * @param byteLength - 随机字节数。
 * @returns 长度为 byteLength * 2 的小写十六进制字符串。
 */
function randomHex(byteLength: number): string {
  const bytes = new Uint8Array(byteLength);
  getCrypto().getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/**
 * 返回当前运行环境可用的 crypto 对象。
 *
 * @returns Web Crypto 兼容对象。
 * @throws {Error} 当前环境没有 crypto.getRandomValues 时抛出。
 */
function getCrypto(): Crypto {
  const cryptoApi = globalThis.crypto as Crypto | undefined;
  if (cryptoApi && typeof cryptoApi.getRandomValues === "function") {
    return cryptoApi;
  }
  throw new Error("当前环境不支持 crypto.getRandomValues");
}

/**
 * 移除对象中的空字段。
 *
 * @param fields - 可选字段字典。
 * @returns 不含空值的字段字典。
 */
function withoutEmpty<T extends Record<string, unknown>>(fields: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(fields).filter(([, value]) => value !== undefined && value !== null && value !== ""),
  ) as Partial<T>;
}

/**
 * 判断客户端 trace 上下文字段是否发生变化。
 *
 * @param current - 当前 store 中的 trace 上下文。
 * @param next - 合并候选 trace 上下文。
 * @returns 任一字段不同则返回 true。
 */
function hasTraceChange(current: ClientTraceContext, next: ClientTraceContext): boolean {
  return (
    current.traceId !== next.traceId ||
    current.startedAt !== next.startedAt ||
    current.taskId !== next.taskId ||
    current.runId !== next.runId
  );
}
