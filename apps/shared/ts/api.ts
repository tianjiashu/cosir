/**
 * API 路径常量与请求/响应类型定义。
 *
 * 与后端 `app/api/app.py` 路由保持一致，
 * 作为前后端共享的 HTTP 契约事实源。
 *
 * @module shared/api
 */

/** 后端 API 基础路径前缀。 */
export const API_BASE = "/api";

/** 任务相关 API 路径常量，与后端路由一一对应。 */
export const API_PATHS = {
  /** GET — 后端健康状态与当前模型配置 */
  HEALTH: "/health",
  /** GET — 工作区列表 */
  WORKSPACES: "/workspaces",
  /** DELETE — 删除工作区 */
  WORKSPACE_DETAIL: (workspaceId: string) => `/workspaces/${workspaceId}`,
  /** GET/POST — 工作区下任务列表与创建 */
  WORKSPACE_TASKS: (workspaceId: string) => `/workspaces/${workspaceId}/tasks`,
  /** GET — 查询任务状态（含 task_id 参数） */
  TASK_DETAIL: (taskId: string) => `/tasks/${taskId}`,
  /** GET/POST — 任务下轮次列表与追加 */
  TASK_TURNS: (taskId: string) => `/tasks/${taskId}/turns`,
  /** GET — 历史事件列表 */
  TASK_EVENTS: (taskId: string) => `/tasks/${taskId}/events`,
  /** GET — turn 级 SSE 事件流 */
  TURN_STREAM: (turnId: string) => `/turns/${turnId}/stream`,
  /** POST — 取消任务 */
  TASK_CANCEL: (taskId: string) => `/tasks/${taskId}/cancel`,
  /** GET — 按 trace 查询日志 */
  LOGS_QUERY: "/logs/query",
  /** GET — 查询最近日志 */
  LOGS_RECENT: "/logs/recent",
} as const;

// ---------- 请求类型 ----------

/** 创建任务的请求体，对应后端 `CreateTaskRequest`。 */
export interface CreateTaskRequest {
  /** 非空的纯文本任务输入。 */
  text: string;
  /** 工作区标识符。 */
  workspace_id: string;
}

/** 创建工作区的请求体，对应后端 `CreateWorkspaceRequest`。 */
export interface CreateWorkspaceRequest {
  /** 非空的工作区名称。 */
  name: string;
  /** 非空的工作区本地路径。 */
  root_path: string;
}

/** 创建轮次的请求体，对应后端 `CreateTurnRequest`。 */
export interface CreateTurnRequest {
  /** 非空的本轮用户输入。 */
  input_text: string;
}

// ---------- 响应类型 ----------

/** 后端健康状态响应。 */
export interface BackendHealthResponse {
  /** 后端服务状态。 */
  status: string;
  /** 当前有效的模型服务商。 */
  model_provider: string;
  /** 当前模型 API base URL。 */
  model_base_url: string;
  /** 当前模型名。 */
  model_name: string;
  /** 当前 thinking 模式。 */
  model_thinking_mode: string;
  /** 当前读取 API Key 的环境变量名。 */
  model_api_key_env: string;
  /** 当前进程里是否已经拿到 API Key。 */
  has_model_api_key: boolean;
}

/** 创建任务 / 查询任务 的响应体（即 TaskRecord）。 */
export type TaskResponse = import("./task").TaskRecord;

/** 创建工作区 / 查询工作区 的响应体。 */
export type WorkspaceResponse = import("./workspace").WorkspaceRecord;

/** 创建轮次 / 查询轮次 的响应体。 */
export type TurnResponse = import("./turn").TurnRecord;

/** 事件列表的响应体。 */
export type EventsResponse = import("./events").RuntimeEvent[];

/** 日志查询响应体。 */
export type LogQueryResponse = import("./logs").LogQueryResponse;
