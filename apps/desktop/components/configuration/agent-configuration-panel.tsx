import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useStore } from "zustand";
import { ChevronLeftIcon, Code2Icon, Loader2Icon, PlusIcon, SaveIcon, Settings2Icon, ShieldCheckIcon, Trash2Icon } from "lucide-react";

import { AgentConfigurationForm } from "@/components/configuration/agent-configuration-form";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import type { ToolGroupCatalog } from "@/lib/api/tools";
import { configurationScope, createAgentConfiguration, deleteAgentConfiguration, updateAgentConfiguration, type AgentConfiguration } from "@/lib/api/configuration";
import { modelOptionId, unknownModelName, useModelCatalog, type ModelCatalog } from "@/lib/model/catalog";
import { createAgentEditorSession } from "@/lib/agent-editor/session";
import { confirmDelete } from "@/lib/confirm/confirm-store";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { confirmDiscardUnsaved, useUnsavedChanges } from "@/lib/unsaved-changes";
import { agentConfigurationsQueryOptions, toolGroupsQueryOptions } from "@/lib/query/query-options";

/** 子 Agent 卡片上的模型名：目录就绪后找不到即视为已删除的配置；目录未就绪时只给编号，避免误报「未知」。 */
function agentModelLabel(modelConfigId: number, catalog: ModelCatalog | null): string {
  const model = catalog?.byOptionId.get(modelOptionId(modelConfigId));
  if (model) return model.label;
  return catalog ? unknownModelName(modelConfigId) : `模型 #${modelConfigId}`;
}


/** 离开编辑器会丢弃表单，先经统一的未保存守卫确认。 */
async function leaveEditor(onClose: () => void) {
  if (await confirmDiscardUnsaved("settings")) onClose();
}

function AgentEditor({
  initial,
  toolGroups,
  workspaceId,
  onClose,
}: {
  initial: AgentConfiguration | null;
  toolGroups: ToolGroupCatalog[];
  workspaceId: number | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [session] = useState(() => createAgentEditorSession({
    initial, saved: initial !== null, scope: configurationScope(workspaceId),
  }));
  const dirty = useStore(session.store, (state) => state.dirty);
  const saving = useStore(session.store, (state) => state.saving);
  useUnsavedChanges("settings", initial ? `子 Agent「${initial.agent_id}」` : "新建子 Agent", dirty);

  const mutation = useMutation({
    mutationFn: () => session.save((input, { agentId }) => (
      agentId ? updateAgentConfiguration(workspaceId, agentId, input) : createAgentConfiguration(workspaceId, input)
    )),
    onSuccess: async (saved) => {
      if (!saved) return;
      // 等列表刷新后再关闭编辑器，避免返回时短暂显示旧列表。
      await queryClient.invalidateQueries({ queryKey: queryKeys.agents(workspaceId) });
      notify.success("配置更改成功");
      onClose();
    },
    onError: (error) => {
      if (error instanceof UserFacingError) return;
      void frontendLog("ERROR", "agent_configuration_save_failed", "保存子 Agent 配置失败", {
        data: { agentId: session.store.getState().form.agent_id, workspaceId, isNew: !initial },
        error,
      });
    },
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>{initial ? "编辑子 Agent" : "新建子 Agent"}</CardTitle>
        <CardAction><Button variant="outline" onClick={() => void leaveEditor(onClose)}><ChevronLeftIcon />返回子 Agent 列表</Button></CardAction>
      </CardHeader>
      <CardContent className="space-y-5">
        {mutation.isError && <Alert tone="destructive">{userErrorMessage(mutation.error, "保存失败，请重试")}</Alert>}
        <AgentConfigurationForm session={session} toolGroups={toolGroups} />
        <div className="flex justify-end gap-2 border-t pt-4"><Button variant="outline" onClick={() => void leaveEditor(onClose)}>取消</Button><Button onClick={() => mutation.mutate()} disabled={saving}>{saving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}保存配置</Button></div>
      </CardContent>
    </Card>
  );
}

/** 某一作用域自己的子 Agent 列表与编辑：`workspaceId` 为 null 时是系统级。 */
export function AgentConfigurationPanel({ workspaceId }: { workspaceId: number | null }) {
  const queryClient = useQueryClient();
  const agentsQuery = useQuery({
    ...agentConfigurationsQueryOptions(workspaceId),
  });
  const toolGroupsQuery = useQuery({
    ...toolGroupsQueryOptions(),
  });
  const { catalog } = useModelCatalog();
  const [editing, setEditing] = useState<AgentConfiguration | null | undefined>(undefined);
  const agents = agentsQuery.data;
  const toolGroups = toolGroupsQuery.data?.groups;

  const removeAgent = async (agent: AgentConfiguration) => {
    const removed = await confirmDelete({
      target: `子 Agent「${agent.agent_id}」`,
      consequence: "将删除该子 Agent 的配置文件。",
      action: async () => {
        try {
          await deleteAgentConfiguration(workspaceId, agent.agent_id);
        } catch (error) {
          void frontendLog("ERROR", "agent_configuration_delete_failed", "删除子 Agent 配置失败", {
            data: { agentId: agent.agent_id, workspaceId },
            error,
          });
          throw error;
        }
        await queryClient.invalidateQueries({ queryKey: queryKeys.agents(workspaceId) });
      },
    });
    if (removed) notify.success(`已删除子 Agent「${agent.agent_id}」`);
  };

  if (editing !== undefined && toolGroups) return <AgentEditor workspaceId={workspaceId} toolGroups={toolGroups} initial={editing} onClose={() => setEditing(undefined)} />;
  if (!agents || !toolGroups) {
    const loadError = agentsQuery.error ?? toolGroupsQuery.error;
    return loadError
      ? <ErrorState className="my-10" message={userErrorMessage(loadError, "读取子 Agent 配置失败")} onRetry={() => { void agentsQuery.refetch(); void toolGroupsQuery.refetch(); }} retryLabel="重新读取" />
      : <LoadingState label="正在读取系统配置…" />;
  }
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-end"><Button onClick={() => setEditing(null)}><PlusIcon />新建子 Agent</Button></div>
      <div className="grid gap-3 xl:grid-cols-2">
        {agents.map((agent) => (
          <Card key={`${agent.source}:${agent.agent_id}`} className="p-4 transition-shadow hover:shadow-md">
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 items-center gap-3">
                <div className="bg-primary/10 text-primary flex size-9 shrink-0 items-center justify-center rounded-xl"><Code2Icon className="size-4" /></div>
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <h3 className="min-w-0 truncate font-medium">{agent.agent_id}</h3>
                    <Badge size="sm" tone={agent.source === "builtin" ? "neutral" : "primary"}>
                      {agent.source === "builtin" ? "内置" : "自定义"}
                    </Badge>
                  </div>
                </div>
              </div>
              {agent.editable || agent.deletable ? (
                <div className="flex gap-1">
                  {agent.editable && <Button variant="ghost" size="icon-sm" onClick={() => setEditing(agent)} aria-label={`编辑 ${agent.agent_id}`}><Settings2Icon /></Button>}
                  {agent.deletable && <Button variant="ghost" size="icon-sm" className="text-destructive" onClick={() => void removeAgent(agent)} aria-label={`删除 ${agent.agent_id}`}><Trash2Icon /></Button>}
                </div>
              ) : (
                <span className="text-muted-foreground flex size-7 shrink-0 items-center justify-center" role="img" aria-label="内置子 Agent，不可编辑或删除" title="内置子 Agent，不可编辑或删除"><ShieldCheckIcon className="size-4" /></span>
              )}
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <Badge shape="tag">{agent.allowed_tool_groups.length} 个工具组</Badge>
              <Badge shape="tag" className="max-w-full">
                <span className="truncate">{agent.model_config_id !== null ? agentModelLabel(agent.model_config_id, catalog) : "跟随默认模型"}</span>
              </Badge>
            </div>
            {agent.validation_status !== "valid" && <p className="text-destructive mt-3 text-xs">{agent.validation_error ?? "配置无效"}</p>}
          </Card>
        ))}
      </div>
    </div>
  );
}

