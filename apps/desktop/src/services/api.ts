/**
 * HTTP API 封装层。
 *
 * 封装所有与后端 FastAPI 的 HTTP 通信：
 * - POST /tasks — 创建任务
 * - GET /tasks/{id} — 查询任务状态
 * - GET /tasks/{id}/events — 历史事件列表
 * - GET /tasks/{id}/checkpoints — checkpoint 列表
 * - POST /tasks/{id}/cancel — 取消任务
 *
 * 使用原生 fetch，不引入 axios 等第三方 HTTP 库（对齐技术选型）。
 *
 * @module services/api
 */

import type { RuntimeEvent } from "@shared/events";
import type { TaskRecord, CheckpointRecord } from "@shared/task";
import type { BackendHealthResponse, CreateTaskRequest } from "@shared/api";
import { API_PATHS } from "@shared/api";
import { ServiceError } from "./types";
import { logError, logWarn } from "../lib/logger";
import {
  buildTraceHeaders,
  readBackendTraceHeaders,
  recordBackendTrace,
} from "./tracePropagation";
import { useConversationTraceStore, type ConversationTraceOperation } from "@/stores/conversationTraceStore";

/** 后端基础 URL，开发环境走 Vite 代理。 */
const BASE_URL = "";

/** HTTP 请求使用的 trace 元数据。 */
interface RequestTraceMetadata {
  /** 客户端请求 trace 标识。 */
  traceId: string;
  /** HTTP 方法。 */
  method: string;
  /** 请求路径。 */
  path: string;
  /** 可选任务标识。 */
  taskId?: string;
}

/** 带请求 trace 元数据的 JSON 响应。 */
interface TracedJsonResponse<T> {
  /** 解析后的响应体。 */
  data: T;
  /** 该请求使用的 trace 元数据。 */
  trace: RequestTraceMetadata;
}

/**
 * 构建带错误上下文的 ServiceError。
 *
 * @param message - 人类可读的错误描述。
 * @param path - 导致错误的 API 路径。
 * @param response - 可选的 fetch Response 对象。
 * @param taskId - 可选的关联任务 ID。
 * @returns 构建好的 ServiceError 实例。
 */
async function buildError(
  message: string,
  path: string,
  response?: Response,
  taskId?: string,
): Promise<ServiceError> {
  const statusCode = response?.status ?? 0;
  let detail = message;
  try {
    if (response) {
      const body = await response.clone().json();
      if (body.detail) {
        detail = body.detail;
      }
    }
  } catch (err) {
    void err;
    logWarn("解析错误响应体 JSON 失败", {
      module: "api",
      path,
      statusCode: response?.status,
    });
    // 非 JSON 响应体，使用原始消息
  }

  return new ServiceError(detail, { statusCode, taskId });
}

/**
 * 发送带 JSON body 的 POST 请求。
 *
 * @param path - API 路径。
 * @param data - 请求体数据。
 * @param taskId - 可选的关联任务 ID（用于错误追踪）。
 * @returns 解析后的 JSON 响应。
 * @throws {ServiceError} 当网络请求失败或返回非 2xx 状态码时抛出。
 */
async function post<T>(path: string, data: unknown, taskId?: string): Promise<TracedJsonResponse<T>> {
  let response: Response;
  const requestTrace = buildTraceHeaders({ taskId });
  const requestContext = {
    module: "api",
    task_id: taskId,
    method: "POST",
    path,
    trace_id: requestTrace.trace.traceId,
  };
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...requestTrace.headers },
      body: JSON.stringify(data),
    });
  } catch (err) {
    logError(`网络请求失败: ${path}`, err, requestContext);
    throw new ServiceError(`网络请求失败: ${path}`, {
      taskId,
      cause: err,
    });
  }

  recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));

  if (!response.ok) {
    const error = await buildError(`POST ${path} 失败 (${response.status})`, path, response, taskId);
    logError(`HTTP 请求失败: POST ${path}`, error, {
      ...requestContext,
      status_code: response.status,
    });
    throw error;
  }

  try {
    return {
      data: await response.json(),
      trace: {
        traceId: requestTrace.trace.traceId,
        method: "POST",
        path,
        taskId,
      },
    };
  } catch (err) {
    logError(`解析响应 JSON 失败: ${path}`, err, requestContext);
    throw new ServiceError(`解析响应 JSON 失败: ${path}`, {
      taskId,
      cause: err,
    });
  }
}

/**
 * 发送 GET 请求。
 *
 * @param path - API 路径。
 * @param taskId - 可选的关联任务 ID（用于错误追踪）。
 * @returns 解析后的 JSON 响应。
 * @throws {ServiceError} 当网络请求失败或返回非 2xx 状态码时抛出。
 */
async function get<T>(path: string, taskId?: string): Promise<TracedJsonResponse<T>> {
  let response: Response;
  const requestTrace = buildTraceHeaders({ taskId });
  const requestContext = {
    module: "api",
    task_id: taskId,
    method: "GET",
    path,
    trace_id: requestTrace.trace.traceId,
  };
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      headers: { ...requestTrace.headers },
    });
  } catch (err) {
    logError(`网络请求失败: ${path}`, err, requestContext);
    throw new ServiceError(`网络请求失败: ${path}`, {
      taskId,
      cause: err,
    });
  }

  recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));

  if (!response.ok) {
    const error = await buildError(`GET ${path} 失败 (${response.status})`, path, response, taskId);
    logError(`HTTP 请求失败: GET ${path}`, error, {
      ...requestContext,
      status_code: response.status,
    });
    throw error;
  }

  try {
    return {
      data: await response.json(),
      trace: {
        traceId: requestTrace.trace.traceId,
        method: "GET",
        path,
        taskId,
      },
    };
  } catch (err) {
    logError(`解析响应 JSON 失败: ${path}`, err, requestContext);
    throw new ServiceError(`解析响应 JSON 失败: ${path}`, {
      taskId,
      cause: err,
    });
  }
}

// ---------- 公开 API 函数 ----------

/**
 * 创建一个新任务。
 *
 * @param request - 创建任务的请求体（text + 可选 session_id）。
 * @returns 创建后的任务记录。
 * @throws {ServiceError} 当创建失败时抛出（如 text 为空、网络错误等）。
 *
 * @sideeffect 向后端 POST /tasks 写入一条新的任务记录。
 */
export async function createTask(request: CreateTaskRequest): Promise<TaskRecord> {
  const response = await post<TaskRecord>(API_PATHS.TASKS, request);
  recordConversationTrace(response.trace, "task_create", response.data.task_id);
  return response.data;
}

/**
 * 查询指定任务的状态。
 *
 * @param taskId - 任务标识符。
 * @returns 任务的最新状态记录。
 * @throws {ServiceError} 当任务不存在或网络错误时抛出。
 */
export async function getTask(taskId: string): Promise<TaskRecord> {
  const response = await get<TaskRecord>(API_PATHS.TASK_DETAIL(taskId), taskId);
  recordConversationTrace(response.trace, "task_get", taskId);
  return response.data;
}

/**
 * 获取任务的历史运行时事件列表。
 *
 * @param taskId - 任务标识符。
 * @returns 该任务的有序事件列表。
 * @throws {ServiceError} 当任务不存在或网络错误时抛出。
 */
export async function getTaskEvents(taskId: string): Promise<RuntimeEvent[]> {
  const response = await get<RuntimeEvent[]>(API_PATHS.TASK_EVENTS(taskId), taskId);
  recordConversationTrace(response.trace, "task_events", taskId);
  return response.data;
}

/**
 * 获取任务的 checkpoint 列表。
 *
 * @param taskId - 任务标识符。
 * @returns 该任务的有序 checkpoint 列表。
 * @throws {ServiceError} 当任务不存在或网络错误时抛出。
 */
export async function getTaskCheckpoints(taskId: string): Promise<CheckpointRecord[]> {
  const response = await get<CheckpointRecord[]>(API_PATHS.TASK_CHECKPOINTS(taskId), taskId);
  recordConversationTrace(response.trace, "task_checkpoints", taskId);
  return response.data;
}

/**
 * 取消一个正在运行的任务。
 *
 * @param taskId - 待取消的任务标识符。
 * @returns 取消后的任务记录（status 应为 "cancelled"）。
 * @throws {ServiceError} 当任务不存在或取消失败时抛出。
 *
 * @sideeffect 向后端 POST /tasks/{id}/cancel 更新任务状态为 cancelled。
 */
export async function cancelTask(taskId: string): Promise<TaskRecord> {
  const response = await post<TaskRecord>(API_PATHS.TASK_CANCEL(taskId), {}, taskId);
  recordConversationTrace(response.trace, "task_cancel", taskId);
  return response.data;
}

/**
 * 获取后端健康状态与当前模型配置。
 *
 * @returns 后端健康状态摘要。
 * @throws {ServiceError} 当后端不可达或返回异常状态时抛出。
 */
export async function getBackendHealth(): Promise<BackendHealthResponse> {
  return (await get<BackendHealthResponse>(API_PATHS.HEALTH)).data;
}

/**
 * 记录对话任务 API 请求使用的 trace。
 *
 * @param trace - HTTP helper 返回的请求 trace 元数据。
 * @param operation - 对话请求类型。
 * @param taskId - 该请求归属的任务标识。
 * @returns 无。
 *
 * @sideeffect 写入 conversationTraceStore 内存状态。
 */
function recordConversationTrace(
  trace: RequestTraceMetadata,
  operation: ConversationTraceOperation,
  taskId: string,
): void {
  useConversationTraceStore.getState().recordTrace({
    traceId: trace.traceId,
    taskId,
    approvalId: "",
    operation,
    method: trace.method,
    path: trace.path,
  });
}
