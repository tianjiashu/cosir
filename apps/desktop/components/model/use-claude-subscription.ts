import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import {
  cancelClaudeLogin,
  completeClaudeLogin,
  getClaudeModels,
  getClaudeSubscriptionStatus,
  importClaudeLocalLogin,
  logoutClaudeSubscription,
  startClaudeLogin,
  type ClaudeLoginSession,
  type ClaudeSubscriptionStatus,
} from "@/lib/api/claude-subscription";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { queryKeys } from "@/lib/query/query-keys";

/** 登录凭据归后端；前端只保留短期授权会话，账号变更使旧目录与探活结果失效。 */
export function useClaudeSubscription(enabled: boolean, onCredentialsChanged: () => void) {
  const queryClient = useQueryClient();
  const [login, setLogin] = useState<ClaudeLoginSession | null>(null);
  const pendingLogin = useRef<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [authVersion, setAuthVersion] = useState(0);
  const statusQuery = useQuery({
    queryKey: queryKeys.claudeSubscriptionStatus(),
    queryFn: ({ signal }) => getClaudeSubscriptionStatus({ signal }),
    enabled,
    retry: false,
    refetchInterval: enabled ? 30_000 : false,
  });
  const connected = enabled && statusQuery.data?.status === "connected";
  const modelsQuery = useQuery({
    queryKey: queryKeys.claudeSubscriptionModels(),
    queryFn: ({ signal }) => getClaudeModels({ signal }),
    enabled: connected && !busy,
    retry: false,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    if (!enabled) {
      setLogin(null);
      setError(null);
      return;
    }
    return () => {
      const loginId = pendingLogin.current;
      pendingLogin.current = null;
      if (loginId) {
        void cancelClaudeLogin(loginId).catch((cause) => {
          void frontendLog("WARNING", "claude_login_cancel_failed", "离开配置时未能取消待授权登录", { error: cause });
        });
      }
    };
  }, [enabled]);

  async function credentialsChanged(status: ClaudeSubscriptionStatus) {
    pendingLogin.current = null;
    setLogin(null);
    await queryClient.cancelQueries({ queryKey: queryKeys.claudeSubscriptionModels() });
    queryClient.removeQueries({ queryKey: queryKeys.claudeSubscriptionModels() });
    queryClient.setQueryData(queryKeys.claudeSubscriptionStatus(), status);
    setAuthVersion((version) => version + 1);
    onCredentialsChanged();
  }

  async function perform(operation: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await operation();
    } catch (cause) {
      setError(userErrorMessage(cause, "Claude 账号操作失败，请重试"));
    } finally {
      setBusy(false);
    }
  }

  return {
    statusQuery,
    modelsQuery,
    connected,
    login,
    busy,
    error,
    authVersion,
    start: () => perform(async () => {
      const session = await startClaudeLogin();
      pendingLogin.current = session.login_id;
      setLogin(session);
    }),
    complete: (code: string) => perform(async () => {
      if (!login) return;
      const status = await completeClaudeLogin(login.login_id, code.trim());
      await credentialsChanged(status);
    }),
    cancel: () => perform(async () => {
      if (!login) return;
      await cancelClaudeLogin(login.login_id);
      pendingLogin.current = null;
      setLogin(null);
    }),
    importLocal: () => perform(async () => credentialsChanged(await importClaudeLocalLogin())),
    logout: () => perform(async () => credentialsChanged(await logoutClaudeSubscription())),
  };
}

export type ClaudeSubscriptionConnectionState = ReturnType<typeof useClaudeSubscription>;
