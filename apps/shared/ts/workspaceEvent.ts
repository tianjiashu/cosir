// workspace 状态事件协议（前后端共享）。
//
// 通用 workspace 状态事件通道：当前承载创建时的准备进度（preparing/ready/degraded），
// 后续可扩展其他 workspace 状态事件。枚举值（"workspace_preparing" 等）为线上 SSE 线缆
// 契约，保持向后兼容，勿随意改动。

// workspace 状态事件类型（与后端 EventType.WORKSPACE_* 对齐）。
export type WorkspaceEventType =
  | "workspace_preparing"
  | "workspace_ready"
  | "workspace_degraded";

// 前端归一化状态机的 state 字段字面量（store 与 Badge 使用）。
export type WorkspaceState =
  | "ready"
  | "preparing"
  | "degraded"
  | "error"
  | "idle";

// 后端 degraded 事件携带的降级状态细分（不可用时机的细分）。
export type WorkspaceDegradedState = "unavailable" | "unreachable" | "timeout";

// 前端归一化状态快照（store 与 Badge 使用）。
export interface WorkspaceStatus {
  state: WorkspaceState;
  actionTaken?: "init" | "sync" | "none";
  filesChanged?: number;
  durationMs?: number;
  degradedReason?: string;
  updatedAt: string;
}

// 准备中事件负载：标识正在准备。
export interface WorkspacePreparingPayload {
  workspace_path: string;
}

// 已就绪事件负载：含准备动作摘要。
export interface WorkspaceReadyPayload {
  workspace_path: string;
  action_taken: string;
  files_changed: number;
  duration_ms: number;
}

// 降级事件负载：含降级状态细分与原因（前端据此决定是否降级到文件搜索）。
export interface WorkspaceDegradedPayload {
  workspace_path: string;
  state: WorkspaceDegradedState;
  degraded_reason: string;
}

// workspace 状态事件负载联合类型。
export type WorkspacePayload =
  | WorkspacePreparingPayload
  | WorkspaceReadyPayload
  | WorkspaceDegradedPayload;

// 单条 workspace 状态事件（与后端 WorkspaceEvent.to_dict 对齐）。
export interface WorkspaceEvent {
  event_id: string;
  event_type: WorkspaceEventType;
  workspace_id: string;
  workspace_path: string;
  payload: WorkspacePayload;
  created_at: string;
}

// 是否为 workspace 状态事件类型。
export function isWorkspaceEventType(value: unknown): value is WorkspaceEventType {
  return (
    value === "workspace_preparing" ||
    value === "workspace_ready" ||
    value === "workspace_degraded"
  );
}
