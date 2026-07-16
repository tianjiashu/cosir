/**
 * 后端日志查询 HTTP 服务。
 *
 * @module services/logs
 */

import { API_PATHS, type LogQueryResponse } from "@shared/api";
import type { LogQueryRequest } from "@shared/logs";
import { logError, logInfo } from "../lib/logger";
import { buildTraceHeaders, readBackendTraceHeaders, recordBackendTrace } from "./tracePropagation";
import { ServiceError } from "./types";

/** 后端基础 URL，开发环境走 Vite 代理。 */
const BASE_URL = "";

/**
 * 查询最近后端日志。
 *
 * @param request - 可选日志查询参数。
 * @returns 日志查询响应。
 * @throws {ServiceError} 当后端不可达或返回非 2xx 时抛出。
 *
 * @sideeffect 发起 GET /logs/recent 请求并记录 trace。
 */
export async function fetchRecentLogs(request: Omit<LogQueryRequest, "trace_id"> = {}): Promise<LogQueryResponse> {
  return fetchLogs(API_PATHS.LOGS_RECENT, request, "logs_recent");
}

/**
 * 按 trace_id 查询后端日志。
 *
 * @param request - 包含 trace_id 的日志查询参数。
 * @returns 日志查询响应。
 * @throws {ServiceError} 当 trace_id 为空、后端不可达或返回非 2xx 时抛出。
 *
 * @sideeffect 发起 GET /logs/query 请求并记录 trace。
 */
export async function fetchLogsByTrace(request: LogQueryRequest): Promise<LogQueryResponse> {
  if (!request.trace_id?.trim()) {
    throw new ServiceError("trace_id 不能为空");
  }
  return fetchLogs(API_PATHS.LOGS_QUERY, request, "logs_query");
}

/**
 * 执行日志查询请求。
 *
 * @param path - 日志 API 路径。
 * @param request - 查询参数。
 * @param operation - 日志操作名。
 * @returns 日志查询响应。
 * @throws {ServiceError} 当后端不可达或返回非 2xx 时抛出。
 */
async function fetchLogs(path: string, request: LogQueryRequest, operation: string): Promise<LogQueryResponse> {
  const query = buildQuery(request);
  const url = `${BASE_URL}${path}${query}`;
  const requestTrace = buildTraceHeaders();
  const requestContext = {
    module: "logsService",
    operation,
    method: "GET",
    path,
    trace_id: requestTrace.trace.traceId,
  };
  try {
    const response = await fetch(url, {
      headers: { ...requestTrace.headers },
    });
    recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));
    if (!response.ok) {
      const detail = await readErrorDetail(response);
      throw new ServiceError(`日志查询失败: ${detail || `HTTP ${response.status}`}`, {
        statusCode: response.status,
      });
    }
    const result = (await response.json()) as LogQueryResponse;
    logInfo("日志查询完成", {
      ...requestContext,
      count: result.entries.length,
    });
    return result;
  } catch (error) {
    logError("日志查询失败", error, requestContext);
    if (error instanceof ServiceError) {
      throw error;
    }
    throw new ServiceError("日志查询网络失败", { cause: error });
  }
}

/**
 * 读取后端错误明细。
 *
 * @param response - 非 2xx HTTP 响应。
 * @returns 后端 detail 文本；无法解析时返回空字符串。
 */
async function readErrorDetail(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    return typeof payload.detail === "string" ? payload.detail : "";
  } catch {
    return "";
  }
}

/**
 * 构造查询字符串。
 *
 * @param request - 查询参数。
 * @returns 以 ? 开头的查询字符串；无参数时返回空字符串。
 */
function buildQuery(request: LogQueryRequest): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(request)) {
    if (value === undefined || value === null || value === "") {
      continue;
    }
    params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}
