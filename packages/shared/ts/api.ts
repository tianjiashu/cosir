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
} as const;

// ---------- 请求类型 ----------

/** 创建任务的请求体，对应后端 `CreateTaskRequest`。 */
export interface CreateTaskRequest {
  /** 非空的纯文本任务输入。 */
  text: string;
  /** 可选的会话标识符。 */
  session_id?: string;
}

// ---------- 响应类型 ----------

/** 创建任务 / 查询任务 的响应体（即 TaskRecord）。 */
export type TaskResponse = import("./task").TaskRecord;

/** 事件列表的响应体。 */
export type EventsResponse = import("./events").RuntimeEvent[];

/** checkpoint 列表的响应体。 */
export type CheckpointsResponse = import("./task").CheckpointRecord[];
