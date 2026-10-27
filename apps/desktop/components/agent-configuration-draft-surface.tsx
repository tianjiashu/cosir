"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BotIcon, Loader2Icon, SaveIcon } from "lucide-react";

import {
  AgentModelSelector,
  ModelSettingsEditor,
  NON_ASSIGNABLE_TOOL_GROUPS,
} from "@/components/system-configuration-page";
import { MarkdownSourcePreviewEditor } from "@/components/configuration/markdown-source-preview-editor";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Textarea } from "@/components/ui/textarea";
import { getToolGroups } from "@/lib/api/tools";
import {
  createAgentConfiguration,
  createWorkspaceAgentConfiguration,
  type AgentConfigurationInput,
} from "@/lib/api/configuration";
import {
  modelSettingsFromForm,
  modelSettingsToForm,
  type ModelSettingsForm,
} from "@/lib/model-settings-form";
import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { useWorkbenchStore } from "@/lib/workbench/store";
import type { WorkbenchAgentConfigurationDraftTab } from "@/lib/workbench/types";

type ConfigurationScope = "workspace" | "system";

/**
 * 在 Workbench 中编辑一次工具生成的临时草稿。
 *
 * 组件只持有前端工作副本；只有用户点击保存后才调用既有配置 API。关闭、刷新或对话卡片
 * 本身都不会把草稿写入配置文件或数据库。未保存状态随工作台会话保留，由关闭和工作区切换边界读取。
 */
export function AgentConfigurationDraftSurface({
  tab,
  onClose,
}: {
  tab: WorkbenchAgentConfigurationDraftTab;
  onClose: () => void;
}) {
  const setDraftDirty = useWorkbenchStore((state) => state.setDraftDirty);
  const updateDraft = useWorkbenchStore((state) => state.updateAgentConfigurationDraft);
  const setDraftScope = useWorkbenchStore((state) => state.setAgentConfigurationDraftScope);
  const queryClient = useQueryClient();
  const [scope, setScope] = useState<ConfigurationScope>(tab.scope);
  const [form, setForm] = useState<AgentConfigurationInput>(() => ({
    ...tab.draft,
  }));
  const [modelSettings, setModelSettings] = useState<ModelSettingsForm>(() => modelSettingsToForm(tab.draft.model_settings));
  const selectedToolGroups = useMemo(() => form.allowed_tool_groups, [form.allowed_tool_groups]);

  const toolGroupsQuery = useQuery({
    queryKey: queryKeys.toolGroups(),
    queryFn: ({ signal }) => getToolGroups({ signal }),
  });
  const toolGroups = toolGroupsQuery.data?.groups ?? [];

  const saveMutation = useMutation({
    mutationFn: async (input: AgentConfigurationInput) => {
      if (!Number.isInteger(input.max_steps) || input.max_steps < 1) throw new UserFacingError("最大步数必须是正整数");
      if (scope === "workspace") await createWorkspaceAgentConfiguration(tab.workspaceId, input);
      else await createAgentConfiguration(input);
      return input;
    },
    onSuccess: (input) => {
      updateDraft(tab.id, input);
      setDraftDirty(tab.id, false);
      void queryClient.invalidateQueries({ queryKey: queryKeys.agents(scope === "workspace" ? tab.workspaceId : null) });
      notify.success("配置已保存");
    },
    onError: (cause) => {
      if (cause instanceof UserFacingError) return;
      void frontendLog("ERROR", "agent_configuration_draft_save_failed", "保存子 Agent 配置草稿失败", {
        data: { tabId: tab.id, workspaceId: tab.workspaceId, scope },
        error: cause,
      });
    },
  });

  const setField = <K extends keyof AgentConfigurationInput>(key: K, value: AgentConfigurationInput[K]) => {
    setDraftDirty(tab.id, true);
    setForm((current) => {
      const next = { ...current, [key]: value };
      updateDraft(tab.id, next);
      return next;
    });
  };

  const close = async () => {
    if (tab.dirty) {
      const discard = await requestConfirmation({
        title: "关闭配置草稿？",
        description: "配置草稿尚未保存，关闭后修改会丢失。",
        confirmLabel: "放弃并关闭",
        tone: "destructive",
        icon: "discard",
      });
      if (!discard) return;
    }
    onClose();
  };

  const save = () => {
    saveMutation.mutate({
      ...form,
      allowed_tool_groups: [...selectedToolGroups],
      model_settings: modelSettingsFromForm(modelSettings),
      max_steps: Number(form.max_steps),
    });
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
        <div className="space-y-4">
          {saveMutation.isError && <Alert tone="destructive">{userErrorMessage(saveMutation.error, "保存失败，请重试")}</Alert>}
          <label className="block space-y-1.5 text-sm"><span className="text-muted-foreground">保存范围</span><NativeSelect value={scope} onChange={(event) => { const next = event.target.value as ConfigurationScope; setScope(next); setDraftScope(tab.id, next); setDraftDirty(tab.id, true); }}><option value="workspace">当前工作区</option><option value="system">系统级</option></NativeSelect></label>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1.5 text-sm"><span className="text-muted-foreground">Agent ID</span><Input value={form.agent_id} onChange={(event) => setField("agent_id", event.target.value)} /></label>
            <label className="space-y-1.5 text-sm"><span className="text-muted-foreground">角色</span><Input value={form.role} onChange={(event) => setField("role", event.target.value)} /></label>
          </div>
          <label className="block space-y-1.5 text-sm"><span className="text-muted-foreground">描述</span><Textarea className="h-24 min-h-24 max-h-24 resize-none overflow-y-auto" value={form.description} onChange={(event) => setField("description", event.target.value)} /></label>
          <div className="space-y-1.5 text-sm"><span className="text-muted-foreground">系统提示词</span><MarkdownSourcePreviewEditor value={form.system_prompt} onChange={(value) => setField("system_prompt", value)} initialMode="source" minHeight="14rem" placeholder="定义子 Agent 的工作边界与输出要求…" ariaLabel="子 Agent 配置草稿系统提示词" /></div>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1.5 text-sm"><span className="text-muted-foreground">最大步数</span><Input type="number" min={1} max={10000} value={form.max_steps} onChange={(event) => setField("max_steps", Number(event.target.value))} /></label>
            <div className="space-y-1.5 text-sm"><span className="text-muted-foreground">模型（可选）</span><AgentModelSelector modelConfigId={form.model_config_id} onChange={(value) => setField("model_config_id", value)} /></div>
          </div>
          <div className="space-y-1.5 text-sm"><span className="text-muted-foreground">允许的工具组</span><div className="border-border/70 bg-muted/20 grid gap-2 rounded-xl border p-3">{toolGroupsQuery.isPending && <LoadingState size="compact" label="正在读取工具组…" />}{toolGroupsQuery.isError && <ErrorState message={userErrorMessage(toolGroupsQuery.error, "工具组读取失败")} onRetry={() => void toolGroupsQuery.refetch()} />}{toolGroups.map(({ group }) => { const locked = NON_ASSIGNABLE_TOOL_GROUPS.has(group); return <label key={group} className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm ${locked ? "bg-muted/40 text-muted-foreground cursor-not-allowed" : "bg-background hover:bg-muted/50 cursor-pointer"}`}><Checkbox checked={selectedToolGroups.includes(group)} disabled={locked} onChange={(event) => setField("allowed_tool_groups", event.target.checked ? [...selectedToolGroups, group] : selectedToolGroups.filter((item) => item !== group))} /><span>{group}</span></label>; })}</div></div>
          <ModelSettingsEditor value={modelSettings} onChange={(value) => {
            const nextModelSettings = modelSettingsFromForm(value);
            setModelSettings(value);
            setForm((current) => {
              const next = { ...current, model_settings: nextModelSettings };
              updateDraft(tab.id, next);
              return next;
            });
            setDraftDirty(tab.id, true);
          }} />
          <div className="flex justify-end gap-2 border-t pt-4"><Button type="button" variant="outline" onClick={() => void close()}>关闭</Button><Button type="button" onClick={save} disabled={saveMutation.isPending}>{saveMutation.isPending ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}保存配置</Button></div>
        </div>
      </div>
    </section>
  );
}
