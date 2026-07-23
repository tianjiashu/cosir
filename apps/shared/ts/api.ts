/**
 * 后端 API 共享类型定义。
 *
 * 本文件由 `scripts/generate_api_ts.py` 从后端 Pydantic API schema 生成。
 * 不要手动修改；请先更新 `apps/backend/app/api/schemas/` 后重新生成。
 *
 * @module shared/api
 */
export const API_BASE = "/api";

export const API_PATHS = {
  HEALTH: "/health",
  AGENTS: "/agents",
  WORKSPACES: "/workspaces",
  WORKSPACE_DETAIL: (workspaceId: string) => `/workspaces/${workspaceId}`,
  WORKSPACE_TASKS: (workspaceId: string) => `/workspaces/${workspaceId}/tasks`,
  TASK_DETAIL: (taskId: string) => `/tasks/${taskId}`,
  TASK_TURNS: (taskId: string) => `/tasks/${taskId}/turns`,
  TURN_STREAM: (turnId: string) => `/turns/${turnId}/stream`,
  TURN_CANCEL: (turnId: string) => `/turns/${turnId}/cancel`,
  LOGS_QUERY: "/logs/query",
  LOGS_RECENT: "/logs/recent",
} as const;

export interface CreateTaskRequest {
  text: string;
  workspace_id: string;
}

export interface CreateWorkspaceRequest {
  name: string;
  root_path: string;
}

export interface CreateTurnRequest {
  input_text: string;
  agent_id?: string | null;
}

export interface BackendHealthResponse {
  status: string;
  model_provider: string;
  model_base_url: string;
  model_name: string;
  model_thinking_mode: string;
  model_api_key_env: string;
  has_model_api_key: boolean;
}

export interface DeleteWorkspaceResponse {
  workspace_id: string;
  deleted: boolean;
}

export type TaskResponse = import("./task").TaskRecord;

export type WorkspaceResponse = import("./workspace").WorkspaceRecord;

export type TurnResponse = import("./turn").TurnRecord;

export type LogQueryResponse = import("./logs").LogQueryResponse;

export type AgentProfileResponse = import("./agents").AgentProfileResponse;

export type ListAgentsResponse = import("./agents").ListAgentsResponse;
