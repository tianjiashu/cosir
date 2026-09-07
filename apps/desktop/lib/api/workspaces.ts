import { requestJson } from "@/lib/http/client";

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
  task_type: "user" | "fork" | "delegation" | string;
  fork_available: boolean;
  execution_status: string | null;
  created_at: string;
  updated_at: string;
};
export type StartedConversation = Pick<WorkspaceTask, "task_id">;

export type CreateWorkspaceInput = { name: string; root_path: string };
export type CreateTaskInput = { text: string };

export const getWorkspaces = () => requestJson<Workspace[]>("/workspaces");
export const createWorkspace = (input: CreateWorkspaceInput) =>
  requestJson<Workspace>("/workspaces", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
export const getWorkspaceTasks = (workspaceId: number) => requestJson<WorkspaceTask[]>(`/workspaces/${workspaceId}/tasks`);
export const createWorkspaceTask = (workspaceId: number, input: CreateTaskInput) =>
  requestJson<WorkspaceTask>(`/workspaces/${workspaceId}/tasks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
export const getTask = (taskId: number) => requestJson<WorkspaceTask>(`/tasks/${taskId}`);
export const deleteWorkspace = (workspaceId: number) =>
  requestJson<{ workspace_id: number; deleted: boolean }>(`/workspaces/${workspaceId}`, { method: "DELETE" });
export const deleteTask = (taskId: number) =>
  requestJson<{ task_id: number; deleted: boolean }>(`/tasks/${taskId}`, { method: "DELETE" });
export const forkTask = (taskId: number, runId: number) =>
  requestJson<WorkspaceTask>(`/tasks/${taskId}/fork`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ runId }),
  });
