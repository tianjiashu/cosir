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

/** 后端基础 URL，开发环境走 Vite 代理。 */
const BASE_URL = "";

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
async function post<T>(path: string, data: unknown, taskId?: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
  } catch (err) {
    logError(`网络请求失败: ${path}`, err, { module: "api", taskId, method: "POST" });
    throw new ServiceError(`网络请求失败: ${path}`, {
      taskId,
      cause: err,
    });
  }

  if (!response.ok) {
    throw await buildError(`POST ${path} 失败 (${response.status})`, path, response, taskId);
  }

  try {
    return await response.json();
  } catch (err) {
    logError(`解析响应 JSON 失败: ${path}`, err, { module: "api", taskId, method: "POST" });
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
async function get<T>(path: string, taskId?: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`);
  } catch (err) {
    logError(`网络请求失败: ${path}`, err, { module: "api", taskId, method: "GET" });
    throw new ServiceError(`网络请求失败: ${path}`, {
      taskId,
      cause: err,
    });
  }

  if (!response.ok) {
    throw await buildError(`GET ${path} 失败 (${response.status})`, path, response, taskId);
  }

  try {
    return await response.json();
  } catch (err) {
    logError(`解析响应 JSON 失败: ${path}`, err, { module: "api", taskId, method: "GET" });
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
  return post<TaskRecord>(API_PATHS.TASKS, request);
}

/**
 * 查询指定任务的状态。
 *
 * @param taskId - 任务标识符。
 * @returns 任务的最新状态记录。
 * @throws {ServiceError} 当任务不存在或网络错误时抛出。
 */
export async function getTask(taskId: string): Promise<TaskRecord> {
  return get<TaskRecord>(API_PATHS.TASK_DETAIL(taskId), taskId);
}

/**
 * 获取任务的历史运行时事件列表。
 *
 * @param taskId - 任务标识符。
 * @returns 该任务的有序事件列表。
 * @throws {ServiceError} 当任务不存在或网络错误时抛出。
 */
export async function getTaskEvents(taskId: string): Promise<RuntimeEvent[]> {
  return get<RuntimeEvent[]>(API_PATHS.TASK_EVENTS(taskId), taskId);
}

/**
 * 获取任务的 checkpoint 列表。
 *
 * @param taskId - 任务标识符。
 * @returns 该任务的有序 checkpoint 列表。
 * @throws {ServiceError} 当任务不存在或网络错误时抛出。
 */
export async function getTaskCheckpoints(taskId: string): Promise<CheckpointRecord[]> {
  return get<CheckpointRecord[]>(API_PATHS.TASK_CHECKPOINTS(taskId), taskId);
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
  return post<TaskRecord>(API_PATHS.TASK_CANCEL(taskId), {}, taskId);
}

/**
 * 获取后端健康状态与当前模型配置。
 *
 * @returns 后端健康状态摘要。
 * @throws {ServiceError} 当后端不可达或返回异常状态时抛出。
 */
export async function getBackendHealth(): Promise<BackendHealthResponse> {
  return get<BackendHealthResponse>(API_PATHS.HEALTH);
}
