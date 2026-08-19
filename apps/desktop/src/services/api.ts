/**
 * HTTP API 封装层。
 *
 * 封装与后端 FastAPI 的 HTTP 通信：
 * - POST /workspaces/{workspace_id}/tasks 创建任务容器和首个 turn
 * - GET /tasks/{id} 查询任务状态
 * - GET/POST /tasks/{id}/turns 读取或追加 turn
 * - POST /turns/{id}/cancel 取消当前 turn
 *
 * 普通请求-响应型 API 走 `httpClient.ts` 的 ky 实例（含超时、幂等重试、错误归一为 ServiceError）；
 * SSE 长连接（connectWorkspaceEventStream）仍走原生 fetch，因 ky 不消费 ReadableStream。
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
  WorkspacePrepareResponse,
} from "@shared/api";
import type {
  ModelCandidate,
  ModelCreateRequest,
  ModelEntryRecord,
  ModelImportResult,
  ModelUpdateRequest,
  ProviderConnectionTestResult,
  ProviderCreateRequest,
  ProviderRecord,
  ProviderUpdateRequest,
} from "@shared/model";
import { MODEL_PROVIDER_PATHS } from "@shared/model";
import type { WorkspaceEvent } from "@shared/workspaceEvent";
import { isWorkspaceEventType } from "@shared/workspaceEvent";
import { API_PATHS } from "@shared/api";
import { ServiceError } from "./types";
import { apiClient } from "./httpClient";
import { createSSEFrameParser, type ParsedSSEFrame } from "./sseParser";
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
 * 发送带 JSON body 的 POST 请求。
 *
 * @param path - API 路径。
 * @param data - 请求体数据。
 * @param taskId - 可选的关联任务 ID，用于错误追踪。
 * @param options - 可选请求选项。
 * @param options.timeout - 请求超时时间（毫秒）。省略或传入 `undefined` 时使用 ky 默认超时（30000ms）；
 *   传入 `false` 可禁用前端超时，交由后端护栏控制（用于首次建索引等可能耗时数分钟的长请求）。
 * @returns 解析后的 JSON 响应。
 * @throws {ServiceError} 当网络请求失败或返回非 2xx 状态码时抛出。
 */
async function post<T>(
  path: string,
  data: unknown,
  taskId?: string,
  options?: { timeout?: number | false },
): Promise<TracedJsonResponse<T>> {
  const requestTrace = buildTraceHeaders({ taskId });
  const requestContext = {
    module: "api",
    task_id: taskId,
    method: "POST",
    path,
    trace_id: requestTrace.trace.traceId,
  };
  let response: Response;
  try {
    // ky 在 HTTP 错误时抛经 beforeError 归一后的 ServiceError；此处用标准 Response 读取后端 trace。
    // timeout 透传：undefined 时 ky 用默认 30000ms；false 时禁用前端超时。
    response = await apiClient.post(path, {
      json: data,
      headers: { "Content-Type": "application/json", ...requestTrace.headers },
      timeout: options?.timeout,
    });
  } catch (err) {
    // ky 已将网络/超时/非 2xx 统一为 ServiceError，保留带 module/path 上下文的错误日志。
    logError(`请求失败: POST ${path}`, err, {
      ...requestContext,
      status_code: err instanceof ServiceError ? err.statusCode : undefined,
    });
    throw err;
  }

  recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));

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
 * @param options - 可选请求选项。
 * @param options.timeout - 请求超时时间（毫秒）。省略或传入 `undefined` 时使用 ky 默认超时（30000ms）；
 *   传入 `false` 可禁用前端超时，交由后端护栏控制（用于可能耗时数分钟的长请求）。
 * @returns 解析后的 JSON 响应。
 * @throws {ServiceError} 当网络请求失败或返回非 2xx 状态码时抛出。
 */
async function get<T>(
  path: string,
  taskId?: string,
  options?: { timeout?: number | false },
): Promise<TracedJsonResponse<T>> {
  const requestTrace = buildTraceHeaders({ taskId });
  const requestContext = {
    module: "api",
    task_id: taskId,
    method: "GET",
    path,
    trace_id: requestTrace.trace.traceId,
  };
  let response: Response;
  try {
    // timeout 透传：undefined 时 ky 用默认 30000ms；false 时禁用前端超时。
    response = await apiClient.get(path, {
      headers: { ...requestTrace.headers },
      timeout: options?.timeout,
    });
  } catch (err) {
    logError(`请求失败: GET ${path}`, err, {
      ...requestContext,
      status_code: err instanceof ServiceError ? err.statusCode : undefined,
    });
    throw err;
  }

  recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));

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
 * @param options - 可选请求选项。
 * @param options.timeout - 请求超时时间（毫秒）。省略或传入 `undefined` 时使用 ky 默认超时（30000ms）；
 *   传入 `false` 可禁用前端超时，交由后端护栏控制（用于可能耗时数分钟的长请求）。
 * @returns 解析后的 JSON 响应。
 * @throws {ServiceError} 当网络请求失败或返回非 2xx 状态码时抛出。
 */
async function del<T>(path: string, options?: { timeout?: number | false }): Promise<TracedJsonResponse<T>> {
  const requestTrace = buildTraceHeaders();
  const requestContext = {
    module: "api",
    method: "DELETE",
    path,
    trace_id: requestTrace.trace.traceId,
  };
  let response: Response;
  try {
    // timeout 透传：undefined 时 ky 用默认 30000ms；false 时禁用前端超时。
    response = await apiClient.delete(path, {
      headers: { ...requestTrace.headers },
      timeout: options?.timeout,
    });
  } catch (err) {
    logError(`请求失败: DELETE ${path}`, err, {
      ...requestContext,
      status_code: err instanceof ServiceError ? err.statusCode : undefined,
    });
    throw err;
  }

  recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));

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

/**
 * 发送 PUT 请求。
 *
 * @param path - API 路径。
 * @param data - 请求体数据。
 * @param options - 可选请求选项。
 * @param options.timeout - 请求超时时间（毫秒）。省略或传入 `undefined` 时使用 ky 默认超时（30000ms）；
 *   传入 `false` 可禁用前端超时，交由后端护栏控制（用于可能耗时数分钟的长请求）。
 * @returns 解析后的 JSON 响应。
 * @throws {ServiceError} 当网络请求失败或返回非 2xx 状态码时抛出。
 */
async function put<T>(
  path: string,
  data: unknown,
  options?: { timeout?: number | false },
): Promise<TracedJsonResponse<T>> {
  const requestTrace = buildTraceHeaders();
  const requestContext = {
    module: "api",
    method: "PUT",
    path,
    trace_id: requestTrace.trace.traceId,
  };
  let response: Response;
  try {
    // timeout 透传：undefined 时 ky 用默认 30000ms；false 时禁用前端超时。
    response = await apiClient.put(path, {
      json: data,
      headers: { "Content-Type": "application/json", ...requestTrace.headers },
      timeout: options?.timeout,
    });
  } catch (err) {
    logError(`请求失败: PUT ${path}`, err, {
      ...requestContext,
      status_code: err instanceof ServiceError ? err.statusCode : undefined,
    });
    throw err;
  }

  recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));

  try {
    return {
      data: await response.json(),
      trace: {
        traceId: requestTrace.trace.traceId,
        method: "PUT",
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
 * 触发一次 workspace 状态准备（ensure_ready：init 或增量 sync）。
 *
 * 调用方应先建立 `/events/stream` 的 SSE 订阅再触发本方法，确保 preparing 事件不丢失。
 *
 * @param workspaceId - 工作区标识。
 * @returns workspace 准备结果（ready / state / files_changed / duration_ms 等）。
 * @throws {ServiceError} 当工作区不存在或请求失败时抛出。
 *
 * @sideeffect 向后端 POST /workspaces/{workspace_id}/events/prepare，可能触发一次
 *   CodeGraph init/sync 建索引（大仓库首次可达数分钟）。
 */
export async function prepareWorkspace(workspaceId: string): Promise<WorkspacePrepareResponse> {
  const response = await post<WorkspacePrepareResponse>(
    API_PATHS.WORKSPACE_EVENT_PREPARE(workspaceId),
    {},
    undefined,
    { timeout: false },
  );
  recordConversationTrace(response.trace, "workspace_event_prepare", "");
  return response.data;
}

/**
 * 订阅 workspace 状态事件流（SSE）。
 *
 * 解析 `/workspaces/{id}/events/stream` 返回的 `event:` + `data:` 帧；每收到一条
 * workspace 状态事件即回调。返回的断开函数在组件卸载时调用，避免泄漏连接。
 *
 * 注意：调用方**不应**依赖本函数返回的 promise 去串联后续动作（如触发 prepare）。
 * Tauri WebView 中 workspace 事件流连接后若无数据推送（prepare 尚未触发），
 * ``await fetch`` 可能一直不 resolve，串行依赖会卡住。调用方应将 prepare 与
 * 本订阅**并行**发起（详见 ``workspaceEventStore.startEvent``）。
 *
 * @param workspaceId - 需要订阅状态事件的工作区标识。
 * @param onEvent - 收到状态事件时的回调。
 * @returns 断开连接的清理函数。
 * @throws {ServiceError} 当请求失败时抛出（同步建立连接失败）。
 *
 * @sideeffect 建立一条到后端的 SSE 长连接，直至返回的清理函数被调用或后端推送终态。
 */
export async function connectWorkspaceEventStream(
  workspaceId: string,
  onEvent: (event: WorkspaceEvent) => void,
): Promise<() => void> {
  const abortController = new AbortController();
  const requestTrace = buildTraceHeaders({});
  const requestContext = {
    module: "api",
    workspace_id: workspaceId,
    method: "GET",
    path: API_PATHS.WORKSPACE_EVENT_STREAM(workspaceId),
    trace_id: requestTrace.trace.traceId,
  };
  const response = await fetch(`${BASE_URL}${API_PATHS.WORKSPACE_EVENT_STREAM(workspaceId)}`, {
    signal: abortController.signal,
    headers: { Accept: "text/event-stream", ...requestTrace.headers },
  });

  recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));

  if (!response.ok || !response.body) {
    const error = new ServiceError(
      `连接 workspace 状态事件流失败: HTTP ${response.status}`,
      { cause: new Error(response.statusText) },
    );
    logError("HTTP 请求失败: GET events/stream", error, {
      ...requestContext,
      status_code: response.status,
    });
    abortController.abort();
    throw error;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  // 流式帧解析器：内部缓冲拼接分片，完整帧同步回调（替代手写 split("\n\n") 切帧）
  const frameParser = createSSEFrameParser((frame) => {
    const parsed = parseWorkspaceEvent(frame, workspaceId);
    if (parsed) {
      onEvent(parsed);
    }
  });

  const readLoop = async (): Promise<void> => {
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        frameParser.feed(decoder.decode(value, { stream: true }));
      }
      // EOF：喂入终止空行促使缓冲区内已完整帧被分发（模拟标准流终止），
      // 随后 reset 释放解析器内部状态。不要用 reset({ consume: true })，
      // 那会把不完整残片也当完整帧分发，改变语义。
      frameParser.feed("\n\n");
      frameParser.reset();
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        logError("workspace 状态事件流读取失败", err, requestContext);
      }
    }
  };

  void readLoop();

  return () => {
    abortController.abort();
  };
}

/**
 * 解析单条 workspace 状态事件 SSE 帧。
 *
 * 对已拆分的 SSE 帧按 workspace 状态事件类型校验并 JSON.parse。
 * 帧的 event 名与 data 字符串已由 createSSEFrameParser 保证非空（缺 event 名的帧不会分发）。
 *
 * @param frame - 已拆分的 SSE 帧（含 eventType 与原始 data 字符串）。
 * @param workspaceId - 该帧所属的工作区标识，用于失败日志定位。
 * @returns 解析成功返回 WorkspaceEvent；事件类型非法或 JSON 格式无效返回 null。
 *
 * @sideeffect JSON 解析失败时写 warn 日志（含 workspace_id、事件类型与 data 预览）。
 */
function parseWorkspaceEvent(frame: ParsedSSEFrame, workspaceId: string): WorkspaceEvent | null {
  if (!isWorkspaceEventType(frame.eventType)) {
    return null;
  }
  try {
    return JSON.parse(frame.data) as WorkspaceEvent;
  } catch (err) {
    logWarn("workspace 状态事件 JSON 解析失败", {
      module: "api",
      workspace_id: workspaceId,
      event_type: frame.eventType,
      data_preview: frame.data.slice(0, 200),
      error: err instanceof Error ? err.message : String(err),
    });
    return null;
  }
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

// ---------- 模型 / 厂商配置中心 API ----------

/**
 * 获取当前可用模型候选列表（provider 配置中心解析后的最终可选模型）。
 *
 * @returns 模型候选列表。
 * @throws {ServiceError} 当后端不可达或响应异常时抛出。
 */
export async function listModels(): Promise<ModelEntryRecord[]> {
  return (await get<ModelEntryRecord[]>(MODEL_PROVIDER_PATHS.MODELS)).data;
}

/**
 * 获取已注册的模型厂商列表。
 *
 * @returns 厂商记录列表。
 * @throws {ServiceError} 当后端不可达或响应异常时抛出。
 */
export async function listProviders(): Promise<ProviderRecord[]> {
  return (await get<ProviderRecord[]>(MODEL_PROVIDER_PATHS.PROVIDERS)).data;
}

/**
 * 创建一个新模型厂商。
 *
 * @param request - 厂商创建请求体（含类型与连接配置）。
 * @returns 创建后的厂商记录。
 * @throws {ServiceError} 当创建失败（如配置校验未过）时抛出。
 *
 * @sideeffect 向后端 POST /providers 写入一条新的厂商记录。
 */
export async function createProvider(request: ProviderCreateRequest): Promise<ProviderRecord> {
  const response = await post<ProviderRecord>(MODEL_PROVIDER_PATHS.PROVIDERS, request);
  recordConversationTrace(response.trace, "provider_create", "");
  return response.data;
}

/**
 * 更新指定模型厂商的配置。
 *
 * @param providerId - 待更新的厂商标识。
 * @param request - 厂商更新请求体。
 * @returns 更新后的厂商记录。
 * @throws {ServiceError} 当厂商不存在或更新失败时抛出。
 *
 * @sideeffect 向后端 PUT /providers/{id} 更新厂商记录。
 */
export async function updateProvider(
  providerId: string,
  request: ProviderUpdateRequest,
): Promise<ProviderRecord> {
  const response = await put<ProviderRecord>(MODEL_PROVIDER_PATHS.PROVIDER_DETAIL(providerId), request);
  recordConversationTrace(response.trace, "provider_update", "");
  return response.data;
}

/**
 * 删除指定模型厂商。
 *
 * @param providerId - 待删除的厂商标识。
 * @returns 无。
 * @throws {ServiceError} 当厂商不存在或删除失败时抛出。
 *
 * @sideeffect 向后端 DELETE /providers/{id} 删除厂商记录。
 */
export async function deleteProvider(providerId: string): Promise<void> {
  const response = await del<{ deleted: boolean }>(MODEL_PROVIDER_PATHS.PROVIDER_DETAIL(providerId));
  recordConversationTrace(response.trace, "provider_delete", "");
}

/**
 * 探测指定厂商可获取的模型列表（不写入，仅发现）。
 *
 * @param providerId - 待探测的厂商标识。
 * @returns 探测到的模型候选列表。
 * @throws {ServiceError} 当厂商不存在或探测失败时抛出。
 */
export async function discoverProviderModels(providerId: string): Promise<ModelCandidate[]> {
  return (await post<ModelCandidate[]>(MODEL_PROVIDER_PATHS.PROVIDER_DISCOVER(providerId), {})).data;
}

/**
 * 测试厂商连通性（用已配置参数发起最小 chat 请求，设计文档 §三 用户视角三件套）。
 *
 * 与 discover 不同：测试本身就是「试错」语义，失败也是正常结果之一——
 * 后端返回 200 + ``success=false`` + 稳定 ``error_code`` + 可读 ``error_message``，
 * 前端据此展示修复引导而非抛错。
 *
 * @param providerId - 待测试的厂商标识。
 * @returns 测试结果值对象（成功时 error_code/error_message 为 null；骨架阶段
 *   后端可能返回 501 Not Implemented，由 httpClient 抛 ServiceError）。
 * @throws {ServiceError} 当厂商不存在 / 后端骨架未实现（501） / 网络错误时抛出。
 *
 * @sideeffect 向后端 POST /providers/{id}/test 发起一次最小 chat 请求到厂商端点。
 */
export async function testProviderConnection(
  providerId: string,
): Promise<ProviderConnectionTestResult> {
  return (
    await post<ProviderConnectionTestResult>(
      MODEL_PROVIDER_PATHS.PROVIDER_TEST(providerId),
      {},
    )
  ).data;
}

/**
 * 将模型条目批量导入为可用模型（候选/手动录入均归一为创建请求）。
 *
 * @param providerId - 目标厂商标识。
 * @param models - 待导入的模型创建请求列表。
 * @returns 导入结果（成功条数、跳过条数等）。
 * @throws {ServiceError} 当导入失败时抛出。
 *
 * @sideeffect 向后端 POST /providers/{id}/models 写入模型条目。
 */
export async function importProviderModels(
  providerId: string,
  models: ModelCreateRequest[],
): Promise<ModelImportResult> {
  return (await post<ModelImportResult>(MODEL_PROVIDER_PATHS.PROVIDER_MODELS(providerId), { models })).data;
}

/**
 * 更新单个模型条目的启用状态或别名。
 *
 * @param modelId - 待更新的模型标识。
 * @param request - 模型更新请求体。
 * @returns 更新后的模型记录。
 * @throws {ServiceError} 当模型不存在或更新失败时抛出。
 *
 * @sideeffect 向后端 PUT /models/{id} 更新模型记录。
 */
export async function updateModel(
  modelId: string,
  request: ModelUpdateRequest,
): Promise<ModelEntryRecord> {
  const response = await put<ModelEntryRecord>(MODEL_PROVIDER_PATHS.MODEL_DETAIL(modelId), request);
  recordConversationTrace(response.trace, "model_update", "");
  return response.data;
}

/**
 * 删除单个模型条目。
 *
 * @param modelId - 待删除的模型标识。
 * @returns 无。
 * @throws {ServiceError} 当模型不存在或删除失败时抛出。
 *
 * @sideeffect 向后端 DELETE /models/{id} 删除模型记录。
 */
export async function deleteModel(modelId: string): Promise<void> {
  const response = await del<{ deleted: boolean }>(MODEL_PROVIDER_PATHS.MODEL_DETAIL(modelId));
  recordConversationTrace(response.trace, "model_delete", "");
}
