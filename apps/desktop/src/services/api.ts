/**
 * HTTP API 封装层。
 *
 * 封装与后端 FastAPI 的 HTTP 通信：
 * - POST /workspaces/{workspace_id}/tasks 创建任务容器和首个 turn
 * - GET /tasks/{id} 查询任务状态
 * - GET/POST /tasks/{id}/turns 读取或追加 turn
 * - POST /turns/{id}/cancel 取消当前 turn
 *
 * 使用原生 fetch，不引入 axios 等第三方 HTTP 库。
 *
 * @module services/api
 */

import type { TaskRecord } from "@shared/task";
import type { TurnRecord } from "@shared/turn";
import type { WorkspaceRecord } from "@shared/workspace";
import type { RuntimeEvent } from "@shared/events";
import type {
  BackendHealthResponse,
  ChangeSet,
  CreateTaskRequest,
  CreateTurnRequest,
  CreateWorkspaceRequest,
  DeleteTaskResponse,
  ListAgentsResponse,
} from "@shared/api";
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
    // 非 JSON 响应体，使用原始消息。
  }

  return new ServiceError(detail, { statusCode, taskId });
}

/**
 * 发送带 JSON body 的 POST 请求。
 *
 * @param path - API 路径。
 * @param data - 请求体数据。
 * @param taskId - 可选的关联任务 ID，用于错误追踪。
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
 * @param taskId - 可选的关联任务 ID，用于错误追踪。
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

/**
 * 发送 DELETE 请求。
 *
 * @param path - API 路径。
 * @returns 解析后的 JSON 响应。
 * @throws {ServiceError} 当网络请求失败或返回非 2xx 状态码时抛出。
 */
async function del<T>(path: string): Promise<TracedJsonResponse<T>> {
  let response: Response;
  const requestTrace = buildTraceHeaders();
  const requestContext = {
    module: "api",
    method: "DELETE",
    path,
    trace_id: requestTrace.trace.traceId,
  };
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      method: "DELETE",
      headers: { ...requestTrace.headers },
    });
  } catch (err) {
    logError(`网络请求失败: ${path}`, err, requestContext);
    throw new ServiceError(`网络请求失败: ${path}`, { cause: err });
  }

  recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));

  if (!response.ok) {
    const error = await buildError(`DELETE ${path} 失败 (${response.status})`, path, response);
    logError(`HTTP 请求失败: DELETE ${path}`, error, {
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
        method: "DELETE",
        path,
      },
    };
  } catch (err) {
    logError(`解析响应 JSON 失败: ${path}`, err, requestContext);
    throw new ServiceError(`解析响应 JSON 失败: ${path}`, { cause: err });
  }
}

// ---------- 公开 API 函数 ----------

/**
 * 创建一个新任务。
 *
 * @param request - 创建任务的请求体，包含 text 与 workspace_id。
 * @returns 创建后的任务记录。
 * @throws {ServiceError} 当创建失败时抛出，例如 text 为空或网络错误。
 *
 * @sideeffect 向后端 POST /workspaces/{workspace_id}/tasks 写入一条新的任务记录。
 */
export async function createTask(request: CreateTaskRequest): Promise<TaskRecord> {
  const path = API_PATHS.WORKSPACE_TASKS(request.workspace_id);
  const response = await post<TaskRecord>(path, request);
  recordConversationTrace(response.trace, "task_create", response.data.task_id);
  return response.data;
}

/**
 * 获取工作区列表。
 *
 * @returns 后端登记的工作区记录列表。
 * @throws {ServiceError} 当后端不可达或响应异常时抛出。
 */
export async function listWorkspaces(): Promise<WorkspaceRecord[]> {
  return (await get<WorkspaceRecord[]>(API_PATHS.WORKSPACES)).data;
}

/**
 * 创建本地工作区。
 *
 * @param request - 工作区创建请求体。
 * @returns 创建后的工作区记录。
 * @throws {ServiceError} 当创建失败时抛出。
 */
export async function createWorkspace(request: CreateWorkspaceRequest): Promise<WorkspaceRecord> {
  const response = await post<WorkspaceRecord>(API_PATHS.WORKSPACES, request);
  useConversationTraceStore.getState().recordTrace({
    traceId: response.trace.traceId,
    taskId: "",
    operation: "workspace_create",
    method: response.trace.method,
    path: response.trace.path,
  });
  return response.data;
}

/**
 * 删除工作区及其任务记录。
 *
 * @param workspaceId - 待删除的工作区标识。
 * @returns 无。
 * @throws {ServiceError} 当工作区不存在或删除失败时抛出。
 */
export async function deleteWorkspace(workspaceId: string): Promise<void> {
  const response = await del<{ deleted: boolean }>(API_PATHS.WORKSPACE_DETAIL(workspaceId));
  useConversationTraceStore.getState().recordTrace({
    traceId: response.trace.traceId,
    taskId: "",
    operation: "workspace_delete",
    method: "DELETE",
    path: API_PATHS.WORKSPACE_DETAIL(workspaceId),
  });
}

/**
 * 删除单个任务及其级联的轮次与事件记录。
 *
 * @param taskId - 待删除的任务标识。
 * @returns 无。
 * @throws {ServiceError} 当任务不存在或删除失败时抛出。
 */
export async function deleteTask(taskId: string): Promise<void> {
  const response = await del<DeleteTaskResponse>(API_PATHS.TASK_DETAIL(taskId));
  useConversationTraceStore.getState().recordTrace({
    traceId: response.trace.traceId,
    taskId,
    operation: "task_delete",
    method: "DELETE",
    path: API_PATHS.TASK_DETAIL(taskId),
  });
}

/**
 * 获取工作区下的任务列表。
 *
 * @param workspaceId - 工作区标识。
 * @returns 任务记录列表。
 * @throws {ServiceError} 当工作区不存在或请求失败时抛出。
 */
export async function listWorkspaceTasks(workspaceId: string): Promise<TaskRecord[]> {
  const response = await get<TaskRecord[]>(API_PATHS.WORKSPACE_TASKS(workspaceId));
  useConversationTraceStore.getState().recordTrace({
    traceId: response.trace.traceId,
    taskId: "",
    operation: "workspace_tasks",
    method: response.trace.method,
    path: response.trace.path,
  });
  return response.data;
}

/**
 * 为已有任务追加一个 pending 轮次。
 *
 * @param taskId - 任务容器标识。
 * @param request - 轮次创建请求体。
 * @returns 创建后的轮次记录。
 * @throws {ServiceError} 当任务不存在或输入非法时抛出。
 */
export async function createTaskTurn(taskId: string, request: CreateTurnRequest): Promise<TurnRecord> {
  const response = await post<TurnRecord>(API_PATHS.TASK_TURNS(taskId), request, taskId);
  recordConversationTrace(response.trace, "turn_create", taskId);
  return response.data;
}

/**
 * 获取任务下的轮次列表。
 *
 * @param taskId - 任务容器标识。
 * @returns 轮次记录列表。
 * @throws {ServiceError} 当任务不存在或请求失败时抛出。
 */
export async function listTaskTurns(taskId: string): Promise<TurnRecord[]> {
  const response = await get<TurnRecord[]>(API_PATHS.TASK_TURNS(taskId), taskId);
  recordConversationTrace(response.trace, "task_turns", taskId);
  return response.data;
}

/**
 * 拉取某 task 的累积文件变更集。
 *
 * @param taskId - 任务标识。
 * @param checkpoint - 可选检查点 turn 标识，只返回到该 turn（含）为止的变更。
 * @returns 变更集（检查点 + 去重后的文件条目）。
 * @throws {ServiceError} 当任务不存在或请求失败时抛出。
 */
export async function fetchChangeSet(taskId: string, checkpoint?: string): Promise<ChangeSet> {
  const query = checkpoint ? `?checkpoint=${encodeURIComponent(checkpoint)}` : "";
  const response = await get<ChangeSet>(`${API_PATHS.TASK_CHANGES(taskId)}${query}`, taskId);
  recordConversationTrace(response.trace, "task_changes", taskId);
  return response.data;
}

/**
 * 把一批文件的最新变更标记为「保留」。
 *
 * @param taskId - 任务标识。
 * @param paths - 待保留的文件路径列表。
 * @returns 操作后的完整变更集，供前端直接替换本地状态。
 * @throws {ServiceError} 当任一路径没有已稳定变更时抛出。
 */
export async function keepChanges(taskId: string, paths: string[]): Promise<ChangeSet> {
  const response = await post<ChangeSet>(API_PATHS.TASK_CHANGES_KEEP(taskId), { paths }, taskId);
  recordConversationTrace(response.trace, "task_changes_keep", taskId);
  return response.data;
}

/**
 * 撤销一批文件的最新变更，把它们还原到变更之前。
 *
 * @param taskId - 任务标识。
 * @param paths - 待撤销的文件路径列表。
 * @returns 操作后的完整变更集，供前端直接替换本地状态。
 * @throws {ServiceError} 当任一路径没有已稳定变更，或反向操作应用失败时抛出。
 */
export async function revertChanges(taskId: string, paths: string[]): Promise<ChangeSet> {
  const response = await post<ChangeSet>(API_PATHS.TASK_CHANGES_REVERT(taskId), { paths }, taskId);
  recordConversationTrace(response.trace, "task_changes_revert", taskId);
  return response.data;
}

/**
 * 拉取某任务下完整运行时事件流（按 turn + sequence 升序），用于打开任务时重建细粒度 timeline。
 *
 * 只读历史回放，不重新执行 Agent；历史对话不可变，调用方可结合 eventStore 做内存缓存。
 *
 * @param taskId - 任务容器标识。
 * @returns 按 (turn_id, sequence) 升序排列的历史事件列表。
 * @throws {ServiceError} 当任务不存在或请求失败时抛出。
 */
export async function listTaskEvents(taskId: string): Promise<RuntimeEvent[]> {
  const response = await get<RuntimeEvent[]>(API_PATHS.TASK_EVENTS(taskId), taskId);
  recordConversationTrace(response.trace, "task_events", taskId);
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
 * 取消一个正在运行的轮次。
 *
 * @param turnId - 待取消的轮次标识符。
 * @param taskId - 可选的归属任务 ID，用于 trace 和日志上下文。
 * @returns 取消后的轮次记录，status 应为 "cancelled"。
 * @throws {ServiceError} 当轮次不存在或取消失败时抛出。
 *
 * @sideeffect 向后端 POST /turns/{id}/cancel，更新轮次状态为 cancelled。
 */
export async function cancelTurn(turnId: string, taskId?: string): Promise<TurnRecord> {
  const response = await post<TurnRecord>(API_PATHS.TURN_CANCEL(turnId), {}, taskId);
  recordConversationTrace(response.trace, "turn_cancel", response.data.task_id || taskId || "");
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
 * 获取后端已注册的 Agent profile 列表。
 *
 * @returns Agent profile 列表与默认 agent 标识。
 * @throws {ServiceError} 当后端不可达或返回异常状态时抛出。
 */
export async function listAgents(): Promise<ListAgentsResponse> {
  return (await get<ListAgentsResponse>(API_PATHS.AGENTS)).data;
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
    operation,
    method: trace.method,
    path: trace.path,
  });
}
