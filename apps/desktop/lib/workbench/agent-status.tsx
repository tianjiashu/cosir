import { focusManager, useQueries, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useSyncExternalStore, type ReactNode } from "react";

import { getTask } from "@/lib/api/workspaces";
import { isTerminalRunStatus } from "@/lib/assistant/contract";
import { frontendLog } from "@/lib/logging/frontend-log";
import { queryKeys } from "@/lib/query/query-keys";
import type { WorkbenchTab } from "@/lib/workbench/types";
import { getBackendRuntimeSnapshot, subscribeBackendRuntime } from "@/src/runtime-config";

export type AgentRunSummary = { runId: number | null; status: string | null };

type AgentStatusContextValue = {
  queries: Map<number, UseQueryResult<AgentRunSummary>>;
  available: boolean;
  invalidate: (taskId: number) => void;
};
const AgentStatusContext = createContext<AgentStatusContextValue | null>(null);
const subscribeVisibility = (notify: () => void) => focusManager.subscribe(notify);
const getVisible = () => focusManager.isFocused();

export function agentStatusPollInterval(status: string | null | undefined): number {
  return status && !isTerminalRunStatus(status) ? 3_000 : 10_000;
}

/**
 * 仅在工作台展开时挂载。每个已打开的 Agent Task 只有一个普通 JSON 查询观察者；
 * 关闭面板即释放查询和请求，完整消息与 SSE 仍只由激活阅读页拥有。
 */
export function WorkbenchAgentStatusProvider({ workspaceId, tabs, children }: {
  workspaceId: number;
  tabs: WorkbenchTab[];
  children: ReactNode;
}) {
  const backend = useSyncExternalStore(subscribeBackendRuntime, getBackendRuntimeSnapshot, getBackendRuntimeSnapshot);
  const visible = useSyncExternalStore(subscribeVisibility, getVisible, () => true);
  const client = useQueryClient();
  const agentTabs = tabs.filter(tab => tab.kind === "agent");
  const results = useQueries({ queries: agentTabs.map(tab => ({
    queryKey: queryKeys.workbenchAgent(workspaceId, tab.taskId, backend.generation),
    queryFn: async ({ signal }: { signal: AbortSignal }): Promise<AgentRunSummary> => {
      try {
        const task = await getTask(tab.taskId, { signal });
        return { runId: task.current_run_id, status: task.execution_status };
      } catch (error) {
        if (!signal.aborted) void frontendLog("WARNING", "workbench_agent_status_failed", "工作台 Agent 状态读取失败", {
          data: { taskId: tab.taskId, generation: backend.generation }, error,
        });
        throw error;
      }
    },
    enabled: backend.available && visible,
    staleTime: 0,
    gcTime: 0,
    refetchOnMount: "always" as const,
    refetchOnWindowFocus: "always" as const,
    // 失败后的恢复由有限 retry、重新展开、窗口可见和后端恢复触发，轮询不绕过重试上限。
    refetchInterval: (query: { state: { status: string; data?: AgentRunSummary } }) =>
      query.state.status === "error" ? false : agentStatusPollInterval(query.state.data?.status),
  })) });
  const invalidate = useCallback((taskId: number) => {
    if (!backend.available || !focusManager.isFocused()) return;
    void client.invalidateQueries({ queryKey: queryKeys.workbenchAgent(workspaceId, taskId, backend.generation) }, { cancelRefetch: false });
  }, [backend.available, backend.generation, client, workspaceId]);
  const queries = new Map(agentTabs.map((tab, index) => [tab.taskId, results[index]]));
  return <AgentStatusContext.Provider value={{ queries, available: backend.available, invalidate }}>{children}</AgentStatusContext.Provider>;
}

export function useWorkbenchAgentStatus(taskId: number) {
  const context = useContext(AgentStatusContext);
  if (!context) throw new Error("Agent 状态必须在工作台查询容器内读取");
  const query = context.queries.get(taskId)!;
  const invalidate = context.invalidate;
  const refresh = useCallback(() => invalidate(taskId), [invalidate, taskId]);
  return { query, available: context.available, refresh };
}
