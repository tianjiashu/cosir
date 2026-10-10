"use client";

import { useEffect, useMemo, useState } from "react";
import { BotIcon, Loader2Icon, SaveIcon, XIcon } from "lucide-react";

import {
  AgentModelSelector,
  ModelSettingsEditor,
  NON_ASSIGNABLE_TOOL_GROUPS,
} from "@/components/system-configuration-page";
import { MarkdownSourcePreviewEditor } from "@/components/configuration/markdown-source-preview-editor";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { getToolGroups, type ToolGroupCatalog } from "@/lib/api/tools";
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
import { useWorkbenchStore } from "@/lib/workbench/store";
import type { WorkbenchAgentConfigurationDraftTab } from "@/lib/workbench/types";

type ConfigurationScope = "workspace" | "system";

function ErrorNotice({ message }: { message: string | null }) {
  if (!message) return null;
  return <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{message}</div>;
}

/**
 * 在 Workbench 中编辑一次工具生成的临时草稿。
 *
 * 组件只持有前端工作副本；只有用户点击保存后才调用既有配置 API。关闭、刷新或对话卡片
 * 本身都不会把草稿写入配置文件或数据库。
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
  const [scope, setScope] = useState<ConfigurationScope>(tab.scope);
  const [toolGroups, setToolGroups] = useState<ToolGroupCatalog[]>([]);
  const [form, setForm] = useState<AgentConfigurationInput>(() => ({
    ...tab.draft,
  }));
  const [modelSettings, setModelSettings] = useState<ModelSettingsForm>(() => modelSettingsToForm(tab.draft.model_settings));
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selectedToolGroups = useMemo(() => form.allowed_tool_groups, [form.allowed_tool_groups]);

  useEffect(() => {
    const controller = new AbortController();
    void getToolGroups({ signal: controller.signal })
      .then(({ groups }) => setToolGroups(groups))
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "工具组读取失败");
      });
    return () => controller.abort();
  }, []);

  const setField = <K extends keyof AgentConfigurationInput>(key: K, value: AgentConfigurationInput[K]) => {
    setSaved(false);
    setDraftDirty(tab.id, true);
    setForm((current) => {
      const next = { ...current, [key]: value };
      updateDraft(tab.id, next);
      return next;
    });
  };

  const close = () => {
    if (tab.dirty && !window.confirm("配置草稿尚未保存，确定关闭吗？")) return;
    onClose();
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const input: AgentConfigurationInput = {
        ...form,
        allowed_tool_groups: [...selectedToolGroups],
        model_settings: modelSettingsFromForm(modelSettings),
        max_steps: Number(form.max_steps),
      };
      if (!Number.isInteger(input.max_steps) || input.max_steps < 1) throw new Error("最大步数必须是正整数");
      const savedAgent = scope === "workspace"
        ? await createWorkspaceAgentConfiguration(tab.workspaceId, input)
        : await createAgentConfiguration(input);
      void savedAgent;
      updateDraft(tab.id, input);
      setSaved(true);
      setDraftDirty(tab.id, false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败，请重试");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="flex h-full min-h-0 flex-col bg-background">
      <header className="flex shrink-0 items-start justify-between gap-3 border-b px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <BotIcon className="size-4 shrink-0" />
          <div className="min-w-0">
            <h2 className="truncate text-sm font-medium">子 Agent 配置草稿</h2>
            <p className="text-muted-foreground truncate text-xs">对话生成，仅保存后生效</p>
          </div>
        </div>
        <Button type="button" variant="ghost" size="icon-sm" aria-label="关闭配置草稿" onClick={close}><XIcon /></Button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="space-y-4">
          <ErrorNotice message={error} />
          {saved && <div role="status" className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-300">配置已保存</div>}
          <label className="block space-y-1.5 text-sm"><span className="text-muted-foreground">保存范围</span><select className="border-input bg-background h-9 w-full rounded-lg border px-3 text-sm" value={scope} onChange={(event) => { const next = event.target.value as ConfigurationScope; setScope(next); setDraftScope(tab.id, next); setDraftDirty(tab.id, true); }}><option value="workspace">当前工作区</option><option value="system">系统级</option></select></label>
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
          <div className="space-y-1.5 text-sm"><span className="text-muted-foreground">允许的工具组</span><div className="border-border/70 bg-muted/20 grid gap-2 rounded-xl border p-3">{toolGroups.map(({ group }) => { const locked = NON_ASSIGNABLE_TOOL_GROUPS.has(group); return <label key={group} className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm ${locked ? "bg-muted/40 text-muted-foreground cursor-not-allowed" : "bg-background hover:bg-muted/50 cursor-pointer"}`}><input type="checkbox" checked={selectedToolGroups.includes(group)} disabled={locked} onChange={(event) => setField("allowed_tool_groups", event.target.checked ? [...selectedToolGroups, group] : selectedToolGroups.filter((item) => item !== group))} /><span>{group}</span></label>; })}</div></div>
          <ModelSettingsEditor value={modelSettings} onChange={(value) => {
            const nextModelSettings = modelSettingsFromForm(value);
            setModelSettings(value);
            setForm((current) => {
              const next = { ...current, model_settings: nextModelSettings };
              updateDraft(tab.id, next);
              return next;
            });
            setDraftDirty(tab.id, true);
            setSaved(false);
          }} />
          <div className="flex justify-end gap-2 border-t pt-4"><Button type="button" variant="outline" onClick={close}>关闭</Button><Button type="button" onClick={() => void save()} disabled={saving}>{saving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}保存配置</Button></div>
        </div>
      </div>
    </section>
  );
}
