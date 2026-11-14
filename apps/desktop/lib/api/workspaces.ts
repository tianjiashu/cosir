/** Wire values of backend SessionType; unknown session kinds are not a supported contract. */
export type SessionType = "user" | "user_fork" | "agent_fork" | "delegated";

import { postJson, requestJson } from "@/lib/http/client";

type ReadRequestOptions = Pick<RequestInit, "signal">;

export type Workspace = {
  workspace_id: number;
  name: string;
  root_path: string;
  created_at: string;
  updated_at: string;
};

type ForkSessionMetadata = {
  tree_parent_session_id: number | null;
};

export type WorkspaceSession = {
  session_id: number;
  workspace_id: number;
  title: string;
  session_type: SessionType;
  extra?: {
    fork?: ForkSessionMetadata | null;
  } | null;
  /**
   * 未开始的会话（用户会话且从未有过 Run）：它就是这个工作区的新会话页，进入时取或建，每个工作区至多一个。
   * 它不进侧栏目录（`useWorkspaceCatalog` 一处分出），首个 Run 建立后原地成为普通会话。
   */
  unstarted: boolean;
  created_at: string;
  updated_at: string;
};
export type CreateWorkspaceInput = { name: string; root_path: string };

export const getWorkspaces = (options?: ReadRequestOptions) => requestJson<Workspace[]>("/workspaces", options);
export const createWorkspace = (input: CreateWorkspaceInput) => postJson<Workspace>("/workspaces", input);
export const getWorkspaceSessions = (workspaceId: number, options?: ReadRequestOptions) => requestJson<WorkspaceSession[]>(`/workspaces/${workspaceId}/sessions`, options);
/** 取这个工作区未开始的会话，没有就建一个（幂等）；开终端、开浏览器标签、发首条消息前调用。 */
export const getOrCreateUnstartedSession = (workspaceId: number) => requestJson<WorkspaceSession>(`/workspaces/${workspaceId}/unstarted-session`, { method: "PUT" });
export const getSession = (sessionId: number, options?: ReadRequestOptions) => requestJson<WorkspaceSession>(`/sessions/${sessionId}`, options);
export const deleteWorkspace = (workspaceId: number) =>
  requestJson<{ workspace_id: number; deleted: boolean }>(`/workspaces/${workspaceId}`, { method: "DELETE" });
export type DeleteSessionOptions = {
  /** 连同全部 fork 后代一起删除；默认只删这一个，它的 fork 改挂到上一级。 */
  withForks?: boolean;
};

/** 返回实际删除的全部 Session id（含委派子 Agent 与一并删除的 fork）。 */
export const deleteSession = (sessionId: number, options: DeleteSessionOptions = {}) => {
  const query = new URLSearchParams();
  if (options.withForks) query.set("withForks", "true");
  const search = query.size > 0 ? `?${query.toString()}` : "";
  return requestJson<{ session_id: number; deleted: boolean; deleted_session_ids: number[] }>(
    `/sessions/${sessionId}${search}`,
    { method: "DELETE" },
  );
};
/** 从这条回复往后分叉：新 Session 继承到它为止的对话。 */
export const forkSession = (sessionId: number, replyMessageId: string) => postJson<WorkspaceSession>(`/sessions/${sessionId}/fork`, { replyMessageId });
