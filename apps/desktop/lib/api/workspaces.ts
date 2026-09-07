import { apiRequest } from "@/lib/api/client";

export type Workspace = {
  workspace_id: number;
  name: string;
  root_path: string;
  created_at: string;
  updated_at: string;
};

export type WorkspaceTask = {
  task_id: number;
  workspace_id: number;
  title: string;
  execution_status: string | null;
  created_at: string;
  updated_at: string;
};
export type StartedConversation = Pick<WorkspaceTask, "task_id">;

export type CreateWorkspaceInput = { name: string; root_path: string };
export type CreateTaskInput = { text: string };

export const getWorkspaces = () => apiRequest<Workspace[]>("/workspaces");
export const createWorkspace = (input: CreateWorkspaceInput) =>
  apiRequest<Workspace>("/workspaces", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
export const getWorkspaceTasks = (workspaceId: number) => apiRequest<WorkspaceTask[]>(`/workspaces/${workspaceId}/tasks`);
export const createWorkspaceTask = (workspaceId: number, input: CreateTaskInput) =>
  apiRequest<WorkspaceTask>(`/workspaces/${workspaceId}/tasks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
export const getTask = (taskId: number) => apiRequest<WorkspaceTask>(`/tasks/${taskId}`);
export const deleteWorkspace = (workspaceId: number) =>
  apiRequest<{ workspace_id: number; deleted: boolean }>(`/workspaces/${workspaceId}`, { method: "DELETE" });
export const deleteTask = (taskId: number) =>
  apiRequest<{ task_id: number; deleted: boolean }>(`/tasks/${taskId}`, { method: "DELETE" });
