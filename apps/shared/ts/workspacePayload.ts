/**
 * 后端 workspace 状态事件 payload 共享类型定义。
 *
 * 本文件由 `scripts/generate_workspace_event_ts.py` 从后端 Pydantic payload 模型生成。
 * 不要手动修改；请先更新 `apps/backend/app/models/payload/workspace_payload/` 后重新生成。
 *
 * @module shared/workspacePayload
 */
export interface WorkspacePreparingPayload {
  workspace_path: string;
}

export interface WorkspaceReadyPayload {
  workspace_path: string;
  action_taken: 'init' | 'sync';
  files_changed: number;
  duration_ms: number;
}

export interface WorkspaceDegradedPayload {
  workspace_path: string;
  state: string;
  degraded_reason: string;
}

export type WorkspacePayload =
  | WorkspacePreparingPayload  | WorkspaceReadyPayload  | WorkspaceDegradedPayload;
