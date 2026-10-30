import { postJson, requestJson, type RequestOptions } from "@/lib/http/client";

export type ClaudeSubscriptionStatus = {
  status: "logged_out" | "connected" | "expired";
  expires_at: number | null;
  can_refresh: boolean;
  account_label: string | null;
};

export type ClaudeLoginSession = {
  login_id: string;
  authorization_url: string;
  expires_at: number;
  browser_opened: boolean;
};

export type ClaudeModelChoice = {
  id: string;
  display_name: string;
  context_window_k: number;
  supports_thinking: boolean;
  supports_reasoning_effort: boolean;
  supports_image: boolean;
};

const ROOT = "/model-configs/claude-subscription";

export const getClaudeSubscriptionStatus = (options?: RequestOptions) =>
  requestJson<ClaudeSubscriptionStatus>(`${ROOT}/status`, options);

export const startClaudeLogin = () => postJson<ClaudeLoginSession>(`${ROOT}/login`, {});

/** 授权码只随这次请求发送，不作为查询键或持久化状态。 */
export const completeClaudeLogin = (loginId: string, code: string) =>
  postJson<ClaudeSubscriptionStatus>(`${ROOT}/complete`, { login_id: loginId, code });

export const cancelClaudeLogin = (loginId: string) =>
  postJson<Record<string, never>>(`${ROOT}/cancel`, { login_id: loginId });

export const importClaudeLocalLogin = () =>
  postJson<ClaudeSubscriptionStatus>(`${ROOT}/import-local`, {});

export const logoutClaudeSubscription = () =>
  requestJson<ClaudeSubscriptionStatus>(`${ROOT}/credentials`, { method: "DELETE" });

export const getClaudeModels = (options?: RequestOptions) =>
  requestJson<{ models: ClaudeModelChoice[] }>(`${ROOT}/models`, options);
