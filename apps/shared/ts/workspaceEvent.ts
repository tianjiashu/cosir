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
//
// ``failed`` 来源说明（§3.8）：后端 ``WorkspacePrepareResponse.state`` 合法值含 ``failed``
// 表示准备/索引失败（区别于 degraded 的「可用但降级」）。此前前端用 ``error`` 表达连接层
// 错误，现将 SSE/prepare 收到的 ``failed`` 状态归一落位为 ``failed``，不再走默认分支被吞为
// ``idle``。「连接失败」等前端侧错误也统一写入 ``failed``（与后端 ``failed`` 语义合并），
// 故删除原 ``error`` 字面量。
// ``accepted`` 不并入本事件状态机：它是 prepare 请求的**同步即时态**
// （``WorkspacePrepareResponse`` 初值 ``state="accepted"``），属请求-响应快照字段，
// 而非 SSE 流出的事件态，仅在前端调用 prepareWorkspace 的响应处理处作为即时快照读取。
export type WorkspaceState =
  | "ready"
  | "preparing"
  | "degraded"
  | "failed"
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

// workspace 状态事件 payload 接口由 `scripts/generate_workspace_event_ts.py` 生成于
// `workspacePayload.ts`（避免手写漂移）；此处仅 re-export。
//
// 必须先 import 再 re-export：`export type { X } from "./y"` 只做「对外转发」，
// 不会把 X 带入本模块作用域，本文件下方 `WorkspaceEvent.payload` 引用
// WorkspacePayload 时会报 TS2304（Cannot find name）。改为先 import 类型
// 再整体 re-export，本模块作用域与对外导出同时具备。
import type {
  WorkspacePreparingPayload,
  WorkspaceReadyPayload,
  WorkspaceDegradedPayload,
  WorkspacePayload,
} from "./workspacePayload";

export type {
  WorkspacePreparingPayload,
  WorkspaceReadyPayload,
  WorkspaceDegradedPayload,
  WorkspacePayload,
};

// 单条 workspace 状态事件（与后端 WorkspaceEvent.to_dict 对齐）。
export interface WorkspaceEvent {
  event_id: string;
  event_type: WorkspaceEventType;
  /** 工作区标识（后端 int 主键，与 WorkspaceResponse.workspace_id 一致）。 */
  workspace_id: number;
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
