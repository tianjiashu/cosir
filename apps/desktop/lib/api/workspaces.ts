import { apiRequest, assistantTransportRequest } from "@/lib/api/client";

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
export type StartConversationInput = {
  commandId: string;
  text: string;
  providerId: number;
  modelName: string;
  reasoningEffort?: string | null;
};

export const getWorkspaces = () => apiRequest<Workspace[]>("/workspaces");
export const createWorkspace = (input: CreateWorkspaceInput) =>
  apiRequest<Workspace>("/workspaces", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
export const getWorkspaceTasks = (workspaceId: number) => apiRequest<WorkspaceTask[]>(`/workspaces/${workspaceId}/tasks`);
export const createWorkspaceTask = (workspaceId: number, input: CreateTaskInput) =>
  apiRequest<WorkspaceTask>(`/workspaces/${workspaceId}/tasks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
export const startConversation = (workspaceId: number, input: StartConversationInput) =>
  assistantTransportRequest({
    workspaceId,
    threadId: null,
    commands: [{
      type: "add-message",
      commandId: input.commandId,
      message: { role: "user", parts: [{ type: "text", text: input.text }] },
    }],
    providerId: input.providerId,
    modelName: input.modelName,
    ...(input.reasoningEffort !== undefined && { reasoningEffort: input.reasoningEffort }),
    }).then(async (response) => {
      // 新建页本身还没有 task 对应的 AssistantRuntime。后端已在返回响应前启动
      // ConversationRun，任务页随后通过 canonical state 水合 Assistant UI；这里
      // 只消费身份响应并主动关闭临时订阅，避免把首轮请求阻塞到 Agent 完成。
      const rawTaskId = response.headers.get("X-Cosir-Task-Id");
    const taskId = rawTaskId ? Number(rawTaskId) : NaN;
      if (!Number.isSafeInteger(taskId) || taskId < 1) {
        throw new Error("后端未返回新对话任务标识");
      }
      void response.body?.cancel();
      return {
      task_id: taskId,
    } satisfies StartedConversation;
  });
export const getTask = (taskId: number) => apiRequest<WorkspaceTask>(`/tasks/${taskId}`);
export const deleteWorkspace = (workspaceId: number) =>
  apiRequest<{ workspace_id: number; deleted: boolean }>(`/workspaces/${workspaceId}`, { method: "DELETE" });
export const deleteTask = (taskId: number) =>
  apiRequest<{ task_id: number; deleted: boolean }>(`/tasks/${taskId}`, { method: "DELETE" });
