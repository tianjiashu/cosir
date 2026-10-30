"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useStore } from "zustand";
import { BotIcon, Loader2Icon, SaveIcon } from "lucide-react";

import { AgentConfigurationForm } from "@/components/configuration/agent-configuration-form";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { getToolGroups } from "@/lib/api/tools";
import { createAgentConfiguration, createWorkspaceAgentConfiguration, updateAgentConfiguration, updateWorkspaceAgentConfiguration } from "@/lib/api/configuration";
import type { PersistAgent } from "@/lib/agent-editor/session";
import { confirmCloseWorkbenchTab } from "@/lib/workbench/drafts";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import type { WorkbenchAgentConfigurationDraftTab } from "@/lib/workbench/types";

/**
 * 在 Workbench 中编辑并显式保存一次工具生成的子 Agent 配置草稿。
 *
 * 编辑状态归标签持有的 `AgentEditorSession`，切换标签不会丢失；只有用户点击保存才调用配置 API，
 * 首存 create、之后 update。关闭标签或对话卡片本身都不会写入配置。
 */
export function AgentConfigurationDraftSurface({
  workspaceId,
  tab,
  onClose,
}: {
  workspaceId: number;
  tab: WorkbenchAgentConfigurationDraftTab;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const toolGroupsQuery = useQuery({
    queryKey: queryKeys.toolGroups(),
    queryFn: ({ signal }) => getToolGroups({ signal }),
  });

  const persist: PersistAgent = (input, { scope, agentId }) => {
    if (scope === "workspace") {
      return agentId ? updateWorkspaceAgentConfiguration(workspaceId, agentId, input) : createWorkspaceAgentConfiguration(workspaceId, input);
    }
    return agentId ? updateAgentConfiguration(agentId, input) : createAgentConfiguration(input);
  };
  const saveMutation = useMutation({
    mutationFn: () => tab.session.save(persist),
    onSuccess: (saved) => {
      if (!saved) return;
      const scope = tab.session.store.getState().scope;
      void queryClient.invalidateQueries({ queryKey: queryKeys.agents(scope === "workspace" ? workspaceId : null) });
      notify.success("配置已保存");
    },
    onError: (cause) => {
      if (cause instanceof UserFacingError) return;
      void frontendLog("ERROR", "agent_configuration_draft_save_failed", "保存子 Agent 配置草稿失败", {
        data: { tabId: tab.id, workspaceId, scope: tab.session.store.getState().scope },
        error: cause,
      });
    },
  });

  // 保存状态读会话：面板随标签切换卸载重挂，组件内的 mutation 状态会丢。
  const saving = useStore(tab.session.store, (state) => state.saving);
  const close = async () => {
    if (await confirmCloseWorkbenchTab(tab)) onClose();
  };

  return (
    <section className="flex h-full min-h-0 flex-col bg-background">
      <header className="flex shrink-0 items-start gap-3 border-b px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <BotIcon className="size-4 shrink-0" />
          <div className="min-w-0">
            <h2 className="truncate text-sm font-medium">子 Agent 配置草稿</h2>
            <p className="text-muted-foreground truncate text-xs">对话生成，仅保存后生效</p>
          </div>
        </div>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {toolGroupsQuery.data ? (
          <div className="space-y-4">
            {saveMutation.isError && <Alert tone="destructive">{userErrorMessage(saveMutation.error, "保存失败，请重试")}</Alert>}
            <AgentConfigurationForm session={tab.session} toolGroups={toolGroupsQuery.data.groups} scopeEditable />
            <div className="flex justify-end gap-2 border-t pt-4"><Button type="button" variant="outline" onClick={() => void close()}>关闭</Button><Button type="button" onClick={() => saveMutation.mutate()} disabled={saving}>{saving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}保存配置</Button></div>
          </div>
        ) : toolGroupsQuery.isError ? (
          <ErrorState message={userErrorMessage(toolGroupsQuery.error, "工具组读取失败")} onRetry={() => void toolGroupsQuery.refetch()} />
        ) : <LoadingState label="正在读取工具组…" />}
      </div>
    </section>
  );
}
