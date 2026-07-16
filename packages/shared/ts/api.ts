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
  /** POST — 创建任务 */
  TASKS: "/tasks",
  /** GET — 查询任务状态（含 task_id 参数） */
  TASK_DETAIL: (taskId: string) => `/tasks/${taskId}`,
  /** GET — 历史事件列表 */
  TASK_EVENTS: (taskId: string) => `/tasks/${taskId}/events`,
  /** GET — checkpoint 列表 */
  TASK_CHECKPOINTS: (taskId: string) => `/tasks/${taskId}/checkpoints`,
  /** GET — SSE 事件流（同时触发任务运行） */
  TASK_STREAM: (taskId: string) => `/tasks/${taskId}/stream`,
  /** POST — 取消任务 */
  TASK_CANCEL: (taskId: string) => `/tasks/${taskId}/cancel`,
  /** GET — 可恢复运行列表 */
  RUNS_RECOVERABLE: "/runs/recoverable",
  /** GET — 任务待处理审批 */
  TASK_APPROVALS: (taskId: string) => `/tasks/${taskId}/approvals`,
  /** POST — 提交审批决策 */
  APPROVAL_DECISION: (approvalId: string) => `/approvals/${approvalId}/decision`,
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
  /** 可选的会话标识符。 */
  session_id?: string;
}

/** 审批决策请求体。 */
export interface ApprovalDecisionRequest {
  /** 审批决策，必须是 approved 或 denied。 */
  decision: "approved" | "denied";
  /** 可选的人类可读原因。 */
  reason?: string;
  /** 前端生成的幂等键。 */
  idempotency_key: string;
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

/** 事件列表的响应体。 */
export type EventsResponse = import("./events").RuntimeEvent[];

/** checkpoint 列表的响应体。 */
export type CheckpointsResponse = import("./task").CheckpointRecord[];

/** 可恢复运行列表响应体。 */
export type RecoverableRunsResponse = import("./runs").RunRecord[];

/** 审批列表响应体。 */
export type ApprovalsResponse = import("./approvals").ApprovalRequestRecord[];

/** 审批决策响应体。 */
export type ApprovalDecisionResponse = import("./approvals").ApprovalDecisionRecord;

/** 日志查询响应体。 */
export type LogQueryResponse = import("./logs").LogQueryResponse;
