/**
 * HTTP API 灏佽灞傘€?
 *
 * 灏佽鎵€鏈変笌鍚庣 FastAPI 鐨?HTTP 閫氫俊锛?
 * - POST /workspaces/{workspace_id}/tasks 鈥?鍒涘缓浠诲姟
 * - GET /tasks/{id} 鈥?鏌ヨ浠诲姟鐘舵€?
 * - GET /tasks/{id}/events 鈥?鍘嗗彶浜嬩欢鍒楄〃 * - POST /tasks/{id}/cancel 鈥?鍙栨秷浠诲姟
 *
 * 浣跨敤鍘熺敓 fetch锛屼笉寮曞叆 axios 绛夌涓夋柟 HTTP 搴擄紙瀵归綈鎶€鏈€夊瀷锛夈€?
 *
 * @module services/api
 */

import type { RuntimeEvent } from "@shared/events";
import type { TaskRecord } from "@shared/task";
import type { TurnRecord } from "@shared/turn";
import type { WorkspaceRecord } from "@shared/workspace";
import type { BackendHealthResponse, CreateTaskRequest, CreateTurnRequest, CreateWorkspaceRequest } from "@shared/api";
import { API_PATHS } from "@shared/api";
import { ServiceError } from "./types";
import { logError, logWarn } from "../lib/logger";
import {
  buildTraceHeaders,
  readBackendTraceHeaders,
  recordBackendTrace,
} from "./tracePropagation";
import { useConversationTraceStore, type ConversationTraceOperation } from "@/stores/conversationTraceStore";

/** 鍚庣鍩虹 URL锛屽紑鍙戠幆澧冭蛋 Vite 浠ｇ悊銆?*/
const BASE_URL = "";

/** HTTP 璇锋眰浣跨敤鐨?trace 鍏冩暟鎹€?*/
interface RequestTraceMetadata {
  /** 瀹㈡埛绔姹?trace 鏍囪瘑銆?*/
  traceId: string;
  /** HTTP 鏂规硶銆?*/
  method: string;
  /** 璇锋眰璺緞銆?*/
  path: string;
  /** 鍙€変换鍔℃爣璇嗐€?*/
  taskId?: string;
}

/** 甯﹁姹?trace 鍏冩暟鎹殑 JSON 鍝嶅簲銆?*/
interface TracedJsonResponse<T> {
  /** 瑙ｆ瀽鍚庣殑鍝嶅簲浣撱€?*/
  data: T;
  /** 璇ヨ姹備娇鐢ㄧ殑 trace 鍏冩暟鎹€?*/
  trace: RequestTraceMetadata;
}

/**
 * 鏋勫缓甯﹂敊璇笂涓嬫枃鐨?ServiceError銆?
 *
 * @param message - 浜虹被鍙鐨勯敊璇弿杩般€?
 * @param path - 瀵艰嚧閿欒鐨?API 璺緞銆?
 * @param response - 鍙€夌殑 fetch Response 瀵硅薄銆?
 * @param taskId - 鍙€夌殑鍏宠仈浠诲姟 ID銆?
 * @returns 鏋勫缓濂界殑 ServiceError 瀹炰緥銆?
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
    logWarn("瑙ｆ瀽閿欒鍝嶅簲浣?JSON 澶辫触", {
      module: "api",
      path,
      statusCode: response?.status,
    });
    // 闈?JSON 鍝嶅簲浣擄紝浣跨敤鍘熷娑堟伅
  }

  return new ServiceError(detail, { statusCode, taskId });
}

/**
 * 鍙戦€佸甫 JSON body 鐨?POST 璇锋眰銆?
 *
 * @param path - API 璺緞銆?
 * @param data - 璇锋眰浣撴暟鎹€?
 * @param taskId - 鍙€夌殑鍏宠仈浠诲姟 ID锛堢敤浜庨敊璇拷韪級銆?
 * @returns 瑙ｆ瀽鍚庣殑 JSON 鍝嶅簲銆?
 * @throws {ServiceError} 褰撶綉缁滆姹傚け璐ユ垨杩斿洖闈?2xx 鐘舵€佺爜鏃舵姏鍑恒€?
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
    logError(`缃戠粶璇锋眰澶辫触: ${path}`, err, requestContext);
    throw new ServiceError(`缃戠粶璇锋眰澶辫触: ${path}`, {
      taskId,
      cause: err,
    });
  }

  recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));

  if (!response.ok) {
    const error = await buildError(`POST ${path} 澶辫触 (${response.status})`, path, response, taskId);
    logError(`HTTP 璇锋眰澶辫触: POST ${path}`, error, {
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
    logError(`瑙ｆ瀽鍝嶅簲 JSON 澶辫触: ${path}`, err, requestContext);
    throw new ServiceError(`瑙ｆ瀽鍝嶅簲 JSON 澶辫触: ${path}`, {
      taskId,
      cause: err,
    });
  }
}

/**
 * 鍙戦€?GET 璇锋眰銆?
 *
 * @param path - API 璺緞銆?
 * @param taskId - 鍙€夌殑鍏宠仈浠诲姟 ID锛堢敤浜庨敊璇拷韪級銆?
 * @returns 瑙ｆ瀽鍚庣殑 JSON 鍝嶅簲銆?
 * @throws {ServiceError} 褰撶綉缁滆姹傚け璐ユ垨杩斿洖闈?2xx 鐘舵€佺爜鏃舵姏鍑恒€?
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
    logError(`缃戠粶璇锋眰澶辫触: ${path}`, err, requestContext);
    throw new ServiceError(`缃戠粶璇锋眰澶辫触: ${path}`, {
      taskId,
      cause: err,
    });
  }

  recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));

  if (!response.ok) {
    const error = await buildError(`GET ${path} 澶辫触 (${response.status})`, path, response, taskId);
    logError(`HTTP 璇锋眰澶辫触: GET ${path}`, error, {
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
    logError(`瑙ｆ瀽鍝嶅簲 JSON 澶辫触: ${path}`, err, requestContext);
    throw new ServiceError(`瑙ｆ瀽鍝嶅簲 JSON 澶辫触: ${path}`, {
      taskId,
      cause: err,
    });
  }
}

/**
 * 鍙戦€?DELETE 璇锋眰銆?
 *
 * @param path - API 璺緞銆?
 * @returns 瑙ｆ瀽鍚庣殑 JSON 鍝嶅簲銆?
 * @throws {ServiceError} 褰撶綉缁滆姹傚け璐ユ垨杩斿洖闈?2xx 鐘舵€佺爜鏃舵姏鍑恒€?
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
    logError(`缃戠粶璇锋眰澶辫触: ${path}`, err, requestContext);
    throw new ServiceError(`缃戠粶璇锋眰澶辫触: ${path}`, { cause: err });
  }

  recordBackendTrace(readBackendTraceHeaders(response, requestTrace.trace.traceId));

  if (!response.ok) {
    const error = await buildError(`DELETE ${path} 澶辫触 (${response.status})`, path, response);
    logError(`HTTP 璇锋眰澶辫触: DELETE ${path}`, error, {
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
    logError(`瑙ｆ瀽鍝嶅簲 JSON 澶辫触: ${path}`, err, requestContext);
    throw new ServiceError(`瑙ｆ瀽鍝嶅簲 JSON 澶辫触: ${path}`, { cause: err });
  }
}

// ---------- 鍏紑 API 鍑芥暟 ----------

/**
 * 鍒涘缓涓€涓柊浠诲姟銆?
 *
 * @param request - 鍒涘缓浠诲姟鐨勮姹備綋锛坱ext + workspace_id锛夈€?
 * @returns 鍒涘缓鍚庣殑浠诲姟璁板綍銆?
 * @throws {ServiceError} 褰撳垱寤哄け璐ユ椂鎶涘嚭锛堝 text 涓虹┖銆佺綉缁滈敊璇瓑锛夈€?
 *
 * @sideeffect 鍚戝悗绔?POST /workspaces/{workspace_id}/tasks 鍐欏叆涓€鏉℃柊鐨勪换鍔¤褰曘€?
 */
export async function createTask(request: CreateTaskRequest): Promise<TaskRecord> {
  const path = API_PATHS.WORKSPACE_TASKS(request.workspace_id);
  const response = await post<TaskRecord>(path, request);
  recordConversationTrace(response.trace, "task_create", response.data.task_id);
  return response.data;
}

/**
 * 鑾峰彇宸ヤ綔鍖哄垪琛ㄣ€?
 *
 * @returns 鍚庣鐧昏鐨勫伐浣滃尯璁板綍鍒楄〃銆?
 * @throws {ServiceError} 褰撳悗绔笉鍙揪鎴栧搷搴斿紓甯告椂鎶涘嚭銆?
 */
export async function listWorkspaces(): Promise<WorkspaceRecord[]> {
  return (await get<WorkspaceRecord[]>(API_PATHS.WORKSPACES)).data;
}

/**
 * 鍒涘缓鏈湴宸ヤ綔鍖恒€?
 *
 * @param request - 宸ヤ綔鍖哄垱寤鸿姹備綋銆?
 * @returns 鍒涘缓鍚庣殑宸ヤ綔鍖鸿褰曘€?
 * @throws {ServiceError} 褰撳垱寤哄け璐ユ椂鎶涘嚭銆?
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
 * 鍒犻櫎宸ヤ綔鍖哄強鍏朵换鍔¤褰曘€?
 *
 * @param workspaceId - 寰呭垹闄ょ殑宸ヤ綔鍖烘爣璇嗐€?
 * @returns 鏃犮€?
 * @throws {ServiceError} 褰撳伐浣滃尯涓嶅瓨鍦ㄦ垨鍒犻櫎澶辫触鏃舵姏鍑恒€?
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
 * 鑾峰彇宸ヤ綔鍖轰笅鐨勪换鍔″垪琛ㄣ€?
 *
 * @param workspaceId - 宸ヤ綔鍖烘爣璇嗐€?
 * @returns 浠诲姟璁板綍鍒楄〃銆?
 * @throws {ServiceError} 褰撳伐浣滃尯涓嶅瓨鍦ㄦ垨璇锋眰澶辫触鏃舵姏鍑恒€?
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
 * 涓哄凡鏈変换鍔¤拷鍔犱竴涓?pending 杞銆?
 *
 * @param taskId - 浠诲姟瀹瑰櫒鏍囪瘑銆?
 * @param request - 杞鍒涘缓璇锋眰浣撱€?
 * @returns 鍒涘缓鍚庣殑杞璁板綍銆?
 * @throws {ServiceError} 褰撲换鍔′笉瀛樺湪鎴栬緭鍏ラ潪娉曟椂鎶涘嚭銆?
 */
export async function createTaskTurn(taskId: string, request: CreateTurnRequest): Promise<TurnRecord> {
  const response = await post<TurnRecord>(API_PATHS.TASK_TURNS(taskId), request, taskId);
  recordConversationTrace(response.trace, "turn_create", taskId);
  return response.data;
}

/**
 * 鑾峰彇浠诲姟涓嬬殑杞鍒楄〃銆?
 *
 * @param taskId - 浠诲姟瀹瑰櫒鏍囪瘑銆?
 * @returns 杞璁板綍鍒楄〃銆?
 * @throws {ServiceError} 褰撲换鍔′笉瀛樺湪鎴栬姹傚け璐ユ椂鎶涘嚭銆?
 */
export async function listTaskTurns(taskId: string): Promise<TurnRecord[]> {
  const response = await get<TurnRecord[]>(API_PATHS.TASK_TURNS(taskId), taskId);
  recordConversationTrace(response.trace, "task_turns", taskId);
  return response.data;
}

/**
 * 鏌ヨ鎸囧畾浠诲姟鐨勭姸鎬併€?
 *
 * @param taskId - 浠诲姟鏍囪瘑绗︺€?
 * @returns 浠诲姟鐨勬渶鏂扮姸鎬佽褰曘€?
 * @throws {ServiceError} 褰撲换鍔′笉瀛樺湪鎴栫綉缁滈敊璇椂鎶涘嚭銆?
 */
export async function getTask(taskId: string): Promise<TaskRecord> {
  const response = await get<TaskRecord>(API_PATHS.TASK_DETAIL(taskId), taskId);
  recordConversationTrace(response.trace, "task_get", taskId);
  return response.data;
}

/**
 * 鑾峰彇浠诲姟鐨勫巻鍙茶繍琛屾椂浜嬩欢鍒楄〃銆?
 *
 * @param taskId - 浠诲姟鏍囪瘑绗︺€?
 * @returns 璇ヤ换鍔＄殑鏈夊簭浜嬩欢鍒楄〃銆?
 * @throws {ServiceError} 褰撲换鍔′笉瀛樺湪鎴栫綉缁滈敊璇椂鎶涘嚭銆?
 */
export async function getTaskEvents(taskId: string): Promise<RuntimeEvent[]> {
  const response = await get<RuntimeEvent[]>(API_PATHS.TASK_EVENTS(taskId), taskId);
  recordConversationTrace(response.trace, "task_events", taskId);
  return response.data;
}

/**
 * 鍙栨秷涓€涓鍦ㄨ繍琛岀殑浠诲姟銆?
 *
 * @param taskId - 寰呭彇娑堢殑浠诲姟鏍囪瘑绗︺€?
 * @returns 鍙栨秷鍚庣殑浠诲姟璁板綍锛坰tatus 搴斾负 "cancelled"锛夈€?
 * @throws {ServiceError} 褰撲换鍔′笉瀛樺湪鎴栧彇娑堝け璐ユ椂鎶涘嚭銆?
 *
 * @sideeffect 鍚戝悗绔?POST /tasks/{id}/cancel 鏇存柊浠诲姟鐘舵€佷负 cancelled銆?
 */
export async function cancelTask(taskId: string): Promise<TaskRecord> {
  const response = await post<TaskRecord>(API_PATHS.TASK_CANCEL(taskId), {}, taskId);
  recordConversationTrace(response.trace, "task_cancel", taskId);
  return response.data;
}

/**
 * 鑾峰彇鍚庣鍋ュ悍鐘舵€佷笌褰撳墠妯″瀷閰嶇疆銆?
 *
 * @returns 鍚庣鍋ュ悍鐘舵€佹憳瑕併€?
 * @throws {ServiceError} 褰撳悗绔笉鍙揪鎴栬繑鍥炲紓甯哥姸鎬佹椂鎶涘嚭銆?
 */
export async function getBackendHealth(): Promise<BackendHealthResponse> {
  return (await get<BackendHealthResponse>(API_PATHS.HEALTH)).data;
}

/**
 * 璁板綍瀵硅瘽浠诲姟 API 璇锋眰浣跨敤鐨?trace銆?
 *
 * @param trace - HTTP helper 杩斿洖鐨勮姹?trace 鍏冩暟鎹€?
 * @param operation - 瀵硅瘽璇锋眰绫诲瀷銆?
 * @param taskId - 璇ヨ姹傚綊灞炵殑浠诲姟鏍囪瘑銆?
 * @returns 鏃犮€?
 *
 * @sideeffect 鍐欏叆 conversationTraceStore 鍐呭瓨鐘舵€併€?
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
