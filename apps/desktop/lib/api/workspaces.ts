import { postJson, requestJson } from "@/lib/http/client";

type ReadRequestOptions = Pick<RequestInit, "signal">;

export type Workspace = {
  workspace_id: number;
  name: string;
  root_path: string;
  created_at: string;
  updated_at: string;
};

type ForkTaskMetadata = {
  source_task_id: number;
  /** fork 切分点：源 Task 中那一轮回复所在的消息 id。 */
  source_message_id: string;
};

export type WorkspaceTask = {
  task_id: number;
  workspace_id: number;
  title: string;
  task_type: "user" | "fork" | "delegation" | string;
  extra?: {
    fork?: ForkTaskMetadata;
  } | null;
  execution_status: string | null;
  created_at: string;
  updated_at: string;
};
export type StartedConversation = Pick<WorkspaceTask, "task_id">;

export type CreateWorkspaceInput = { name: string; root_path: string };
export type CreateTaskInput = { text?: string; creationCommandId?: string };

export const getWorkspaces = (options?: ReadRequestOptions) => requestJson<Workspace[]>("/workspaces", options);
export const createWorkspace = (input: CreateWorkspaceInput) => postJson<Workspace>("/workspaces", input);
export const getWorkspaceTasks = (workspaceId: number, options?: ReadRequestOptions) => requestJson<WorkspaceTask[]>(`/workspaces/${workspaceId}/tasks`, options);
export const createWorkspaceTask = (workspaceId: number, input: CreateTaskInput) => postJson<WorkspaceTask>(`/workspaces/${workspaceId}/tasks`, input);
export const getTask = (taskId: number, options?: ReadRequestOptions) => requestJson<WorkspaceTask>(`/tasks/${taskId}`, options);
export const deleteWorkspace = (workspaceId: number) =>
  requestJson<{ workspace_id: number; deleted: boolean }>(`/workspaces/${workspaceId}`, { method: "DELETE" });
export type DeleteTaskOptions = {
  /** 首条命令未被接受时回收 provisional Task 的创建标记（受控清理）。 */
  creationCommandId?: string;
  /** 连同全部 fork 后代一起删除；默认只删这一个，它的 fork 改挂到上一级。 */
  withForks?: boolean;
};

/** 返回实际删除的全部 Task id（含委派子 Agent、Team 节点与一并删除的 fork）。 */
export const deleteTask = (taskId: number, options: DeleteTaskOptions = {}) => {
  const query = new URLSearchParams();
  if (options.creationCommandId) query.set("creationCommandId", options.creationCommandId);
  if (options.withForks) query.set("withForks", "true");
  const search = query.size > 0 ? `?${query.toString()}` : "";
  return requestJson<{ task_id: number; deleted: boolean; deleted_task_ids: number[] }>(
    `/tasks/${taskId}${search}`,
    { method: "DELETE" },
  );
};
/**
 * fork 到某条消息所在那一轮回复为止。`messageId` 可以是用户消息 id、`assistant-{用户消息id}` 或
 * `assistant-run-{runId}`，后端按消息切分。
 */
export const forkTask = (taskId: number, messageId: string) => postJson<WorkspaceTask>(`/tasks/${taskId}/fork`, { messageId });
