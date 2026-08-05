/**
 * CodeGraph 前端共享类型定义。
 *
 * 承载 workspace 索引进度事件（经 `/workspaces/{id}/index/stream` SSE 推送）与
 * 归一化索引状态，供 Sidebar 的「代码索引状态徽标」渲染。事件字段对齐后端
 * `WorkspaceIndexEvent.to_dict()` 与 `Workspace*Payload`；状态为客户端归一化枚举，
 * 不直接透传后端字符串，避免后端演进时破坏前端判断。
 *
 * @module shared/codegraph
 */

/** workspace 索引进度事件类型（对齐后端 EventType.WORKSPACE_*）。 */
export type WorkspaceIndexEventType =
  | "workspace_preparing"
  | "workspace_ready"
  | "workspace_degraded";

/** workspace_preparing 事件载荷。 */
export interface WorkspacePreparingIndexPayload {
  workspace_path: string;
}

/** workspace_ready 事件载荷。 */
export interface WorkspaceReadyIndexPayload {
  workspace_path: string;
  /** 就绪路径动作：init（首次建索引）或 sync（增量同步）。 */
  action_taken: "init" | "sync";
  files_changed: number;
  duration_ms: number;
}

/** workspace_degraded 事件载荷。 */
export interface WorkspaceDegradedIndexPayload {
  workspace_path: string;
  /** 降级来源：failed / unavailable。 */
  state: string;
  degraded_reason: string;
}

/** workspace 索引进度事件载荷联合。 */
export type WorkspaceIndexPayload =
  | WorkspacePreparingIndexPayload
  | WorkspaceReadyIndexPayload
  | WorkspaceDegradedIndexPayload;

/**
 * workspace 索引进度事件（对齐后端 `WorkspaceIndexEvent.to_dict()`）。
 *
 * 与 turn 级 `RuntimeEvent` 不同，本事件无 task_id / turn_id，自带 workspace_id。
 */
export interface WorkspaceIndexEvent {
  event_id: string;
  event_type: WorkspaceIndexEventType;
  workspace_id: string;
  workspace_path: string;
  payload: WorkspaceIndexPayload;
  created_at: string;
}

/**
 * 客户端归一化索引状态。
 *
 * - `idle`：尚未触发索引（新 workspace 或首次加载待触发）。
 * - `preparing`：后端正在 init/sync 建索引。
 * - `ready`：索引就绪，workspace_event 可用。
 * - `degraded`：CodeGraph 不可用，已回退到文件搜索。
 * - `error`：准备接口调用失败（网络/HTTP 异常）。
 */
export type WorkspaceIndexState = "idle" | "preparing" | "ready" | "degraded" | "error";

/** 单 workspace 的索引状态快照（store 中按 workspace_id 存一份）。 */
export interface WorkspaceIndexStatus {
  /** 当前归一化状态。 */
  state: WorkspaceIndexState;
  /** 就绪时动作：init / sync / none。 */
  actionTaken?: "init" | "sync" | "none";
  /** 就绪或降级时的变更文件数。 */
  filesChanged?: number;
  /** 准备耗时（毫秒）。 */
  durationMs?: number;
  /** 降级原因（state 为 degraded 时存在）。 */
  degradedReason?: string;
  /** 最近一次状态更新的本地时间戳。 */
  updatedAt?: string;
}

/** 把事件类型收窄为合法枚举集合。 */
const INDEX_EVENT_TYPES: readonly string[] = [
  "workspace_preparing",
  "workspace_ready",
  "workspace_degraded",
];

/**
 * 判断任意事件类型是否为 workspace 索引进度事件。
 *
 * @param value - 待判定的事件类型字符串。
 * @returns 是索引进度事件类型时返回 true（类型收窄为 `WorkspaceIndexEventType`）。
 */
export function isWorkspaceIndexEventType(value: unknown): value is WorkspaceIndexEventType {
  return typeof value === "string" && (INDEX_EVENT_TYPES as readonly string[]).includes(value);
}
