"use client";

import { useEffect, useMemo, useState } from "react";
import {
  BotIcon,
  CheckIcon,
  ChevronLeftIcon,
  Code2Icon,
  FileTextIcon,
  KeyRoundIcon,
  Loader2Icon,
  PlusIcon,
  SaveIcon,
  Settings2Icon,
  ShieldCheckIcon,
  SlidersHorizontalIcon,
  Trash2Icon,
  UsersIcon,
} from "lucide-react";

import { EnvironmentConfigurationForm, type EnvironmentFieldValue } from "@/components/environment-configuration-form";
import { MarkdownConfigurationPanel } from "@/components/configuration/markdown-configuration-panel";
import { AgentTeamConfigurationPanel } from "@/components/agent-team-configuration-panel";
import { TerminalDenylistConfigurationPanel } from "@/components/terminal-denylist-configuration-panel";
import { MarkdownSourcePreviewEditor } from "@/components/configuration/markdown-source-preview-editor";
import {
  ModelSelectorContent,
  ModelSelectorList,
  ModelSelectorRoot,
  ModelSelectorSearch,
  ModelSelectorTrigger,
  ModelSelectorValue,
  type ModelOption,
} from "@/components/model-selector";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { getToolGroups, type ToolGroupCatalog } from "@/lib/api/tools";
import { modelOptionId, useModelCatalog, type ModelCatalogModel } from "@/lib/model-catalog";
import {
  modelSettingsFromForm,
  modelSettingsToForm,
  type ModelSettingsForm,
} from "@/lib/model-settings-form";
import {
  createAgentConfiguration,
  deleteAgentConfiguration,
  getAgentConfigurations,
  getEnvironmentConfiguration,
  getGlobalInstructionConfiguration,
  getMainAgentPromptConfiguration,
  type AgentConfiguration,
  type AgentConfigurationInput,
  type EnvironmentGroup,
  updateAgentConfiguration,
  updateEnvironmentConfiguration,
  updateGlobalInstructionConfiguration,
  updateMainAgentPromptConfiguration,
} from "@/lib/api/configuration";

type ConfigurationTab = "agents" | "agent-teams" | "main-agent" | "instructions" | "environment" | "terminal-denylist";

export type AgentConfigurationApi = {
  list: () => Promise<AgentConfiguration[]>;
  create: (input: AgentConfigurationInput) => Promise<AgentConfiguration>;
  update: (agentId: string, input: AgentConfigurationInput) => Promise<AgentConfiguration>;
  remove: (agentId: string) => Promise<void>;
};

const systemAgentConfigurationApi: AgentConfigurationApi = {
  list: getAgentConfigurations,
  create: createAgentConfiguration,
  update: updateAgentConfiguration,
  remove: deleteAgentConfiguration,
};

const globalInstructionConfigurationAdapter = {
  load: getGlobalInstructionConfiguration,
  save: updateGlobalInstructionConfiguration,
  allowEmpty: true,
};

const mainAgentPromptConfigurationAdapter = {
  load: getMainAgentPromptConfiguration,
  save: updateMainAgentPromptConfiguration,
  allowEmpty: false,
};

const tabItems: { id: ConfigurationTab; label: string; description: string; icon: typeof Settings2Icon }[] = [
  { id: "agents", label: "子 Agent配置", description: "", icon: SlidersHorizontalIcon },
  { id: "agent-teams", label: "Agent Team 配置", description: "", icon: UsersIcon },
  { id: "main-agent", label: "主 Agent prompt配置", description: "", icon: BotIcon },
  { id: "instructions", label: "全局AGENTS.md配置", description: "", icon: FileTextIcon },
  { id: "environment", label: "环境变量", description: "", icon: KeyRoundIcon },
  { id: "terminal-denylist", label: "终端命令规则", description: "", icon: ShieldCheckIcon },
];

/**
 * 不可授予子 Agent 的工具组：交互终端会话与委派（子 Agent）能力属于主 Agent 的编排职责。
 * 这里保留渲染（让配置者知道存在这些分组），但复选框禁用、不可勾选。
 */
export const NON_ASSIGNABLE_TOOL_GROUPS = new Set(["交互终端工具", "子Agent工具", "配置生成工具"]);

function ErrorNotice({ message }: { message: string | null }) {
  if (!message) return null;
  return <div role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{message}</div>;
}

function LoadingState() {
  return <div className="text-muted-foreground flex items-center gap-2 py-12 text-sm"><Loader2Icon className="size-4 animate-spin" />正在读取系统配置…</div>;
}

function LoadErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <div className="space-y-3 py-12"><ErrorNotice message={message} /><Button variant="outline" onClick={onRetry}>重新读取</Button></div>;
}

const NO_AGENT_MODEL = "__agent_model_default__";

function toAgentModelOption(model: ModelCatalogModel): ModelOption {
  return {
    id: model.optionId,
    name: model.label,
    keywords: [model.configName, model.modelName],
  };
}

export function AgentModelSelector({
  modelConfigId,
  onChange,
}: {
  modelConfigId: number | null;
  onChange: (modelConfigId: number | null) => void;
}) {
  const { catalog, status, retry } = useModelCatalog();
  const modelOptions = useMemo(() => {
    const options: ModelOption[] = [
      { id: NO_AGENT_MODEL, name: "默认", description: "继承主Agent" },
      ...(catalog?.models.map(toAgentModelOption) ?? []),
    ];
    if (modelConfigId === null) return options;
    const currentModelId = modelOptionId(modelConfigId);
    if (options.some((option) => option.id === currentModelId)) return options;
    return [
      ...options,
      { id: currentModelId, name: String(modelConfigId), description: "当前配置（目录中不可用）" },
    ];
  }, [catalog?.models, modelConfigId]);

  if (status === "loading" || status === "idle") {
    return <div className="border-input text-muted-foreground flex h-9 items-center rounded-lg border px-3 text-sm">加载模型…</div>;
  }
  if (status === "error") {
    return <button type="button" className="border-destructive/40 text-destructive hover:bg-destructive/10 flex h-9 w-full items-center rounded-lg border px-3 text-left text-sm" onClick={() => void retry()}>模型加载失败，点击重试</button>;
  }
  if (!catalog || catalog.models.length === 0) {
    return <div className="border-input text-muted-foreground flex h-9 items-center rounded-lg border px-3 text-sm">暂无可用模型</div>;
  }

  const selectedId = modelConfigId !== null ? modelOptionId(modelConfigId) : NO_AGENT_MODEL;
  return (
    <ModelSelectorRoot
      models={modelOptions}
      value={selectedId}
      onValueChange={(nextId) => {
        if (nextId === NO_AGENT_MODEL) {
          onChange(null);
          return;
        }
        const selected = catalog.models.find((model) => model.optionId === nextId);
        if (selected) onChange(selected.modelConfigId);
      }}
    >
      <ModelSelectorTrigger variant="outline" className="w-full max-w-none justify-between" aria-label="选择模型">
        <ModelSelectorValue placeholder="不覆盖" showEffort={false} />
      </ModelSelectorTrigger>
      <ModelSelectorContent searchable className="w-80">
        <ModelSelectorSearch placeholder="搜索模型…" />
        <ModelSelectorList />
      </ModelSelectorContent>
    </ModelSelectorRoot>
  );
}

export function ModelSettingsEditor({
  value,
  onChange,
}: {
  value: ModelSettingsForm;
  onChange: (value: ModelSettingsForm) => void;
}) {
  const setField = <K extends keyof ModelSettingsForm>(field: K, nextValue: ModelSettingsForm[K]) => {
    onChange({ ...value, [field]: nextValue });
  };
  return (
    <div className="border-border/70 bg-muted/20 space-y-3 rounded-xl border p-3">
      <div>
        <p className="text-sm font-medium">模型参数</p>
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="space-y-1 text-xs">
          <span className="text-muted-foreground">温度</span>
          <Input type="number" min="0" step="0.1" value={value.temperature} placeholder="不覆盖" onChange={(event) => setField("temperature", event.target.value)} />
        </label>
        <label className="space-y-1 text-xs">
          <span className="text-muted-foreground">Top P</span>
          <Input type="number" min="0" max="1" step="0.05" value={value.top_p} placeholder="不覆盖" onChange={(event) => setField("top_p", event.target.value)} />
        </label>
        <label className="space-y-1 text-xs">
          <span className="text-muted-foreground">最大输出 Token</span>
          <Input type="number" min="1" step="1" value={value.max_tokens} placeholder="不覆盖" onChange={(event) => setField("max_tokens", event.target.value)} />
        </label>
      </div>
      <div className="grid gap-3 sm:grid-cols-1">
        <label className="space-y-1 text-xs">
          <span className="text-muted-foreground">推理强度</span>
          <select className="border-input bg-background h-9 w-full rounded-lg border px-3 text-sm" value={value.reasoning_effort} onChange={(event) => setField("reasoning_effort", event.target.value)}>
            <option value="">跟随默认</option>
            <option value="low">low</option>
            <option value="high">high</option>
            <option value="max">max</option>
          </select>
        </label>
      </div>
    </div>
  );
}

function AgentEditor({
  initial,
  toolGroups,
  api,
  onCancel,
  onSaved,
}: {
  initial: AgentConfiguration | null;
  toolGroups: ToolGroupCatalog[];
  api: AgentConfigurationApi;
  onCancel: () => void;
  onSaved: (agent: AgentConfiguration) => void;
}) {
  const [form, setForm] = useState<AgentConfigurationInput>(() => ({
    agent_id: initial?.agent_id ?? "",
    role: initial?.role ?? "",
    description: initial?.description ?? "",
    system_prompt: initial?.system_prompt ?? "",
    allowed_tool_groups: initial?.allowed_tool_groups ?? [],
    max_steps: initial?.max_steps ?? 100,
    model_config_id: initial?.model_config_id ?? null,
    model_settings: initial?.model_settings ?? {},
  }));
  const [selectedToolGroups, setSelectedToolGroups] = useState(initial?.allowed_tool_groups ?? []);
  const [modelSettings, setModelSettings] = useState(() => modelSettingsToForm(initial?.model_settings));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const setField = <K extends keyof AgentConfigurationInput>(key: K, value: AgentConfigurationInput[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const input = {
        ...form,
        allowed_tool_groups: selectedToolGroups,
        model_settings: modelSettingsFromForm(modelSettings),
        model_config_id: form.model_config_id === null ? null : Number(form.model_config_id),
      };
      if (input.model_config_id !== null && (!Number.isInteger(input.model_config_id) || input.model_config_id <= 0)) throw new Error("模型配置 ID 必须是正整数");
      const saved = initial
        ? await api.update(initial.agent_id, input)
        : await api.create(input);
      onSaved(saved);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败，请重试");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="border-border/70 bg-card/80 rounded-2xl border shadow-sm">
      <div className="flex items-start justify-between border-b px-5 py-4">
        <div><p className="font-medium">{initial ? "编辑子 Agent" : "新建子 Agent"}</p></div>
        <Button variant="outline" onClick={onCancel}><ChevronLeftIcon />返回 Agent 列表</Button>
      </div>
      <div className="space-y-5 p-5">
        <ErrorNotice message={error} />
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="space-y-1.5 text-sm"><span className="text-muted-foreground">Agent ID</span><Input value={form.agent_id} disabled={Boolean(initial)} onChange={(event) => setField("agent_id", event.target.value)} placeholder="面向主Agent使用的Agent ID" /></label>
          <label className="space-y-1.5 text-sm"><span className="text-muted-foreground">角色</span><Input value={form.role} onChange={(event) => setField("role", event.target.value)} placeholder="给子Agent定义的身份" /></label>
        </div>
        <label className="block space-y-1.5 text-sm"><span className="text-muted-foreground">描述</span><Textarea className="h-24 min-h-24 max-h-24 resize-none overflow-y-auto" value={form.description} onChange={(event) => setField("description", event.target.value)} placeholder="面向主Agent描述这个 Agent 负责什么？" /></label>
        <div className="space-y-1.5 text-sm">
          <span className="text-muted-foreground">系统提示词</span>
          <MarkdownSourcePreviewEditor
            value={form.system_prompt}
            onChange={(value) => setField("system_prompt", value)}
            initialMode="source"
            minHeight="12rem"
            placeholder="定义该子 Agent 的边界、工作方式与输出要求…"
            ariaLabel="子 Agent 系统提示词编辑器"
          />
        </div>
        <div className="grid gap-4 sm:grid-cols-3">
          <label className="space-y-1.5 text-sm"><span className="text-muted-foreground">最大步数</span><Input type="number" min={1} max={10000} value={form.max_steps} onChange={(event) => setField("max_steps", Number(event.target.value))} /></label>
          <div className="space-y-1.5 text-sm sm:col-span-2"><span className="text-muted-foreground">模型 <span className="text-muted-foreground/60">（可选）</span></span><AgentModelSelector modelConfigId={form.model_config_id} onChange={(modelConfigId) => setForm((current) => ({ ...current, model_config_id: modelConfigId }))} /></div>
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="space-y-1.5 text-sm">
            <span className="text-muted-foreground">允许的工具组</span>
            <div className="border-border/70 bg-muted/20 grid gap-2 rounded-xl border p-3 sm:grid-cols-2">
              {toolGroups.map(({ group }) => {
                const locked = NON_ASSIGNABLE_TOOL_GROUPS.has(group);
                return (
                  <label
                    key={group}
                    title={locked ? "子 Agent 不可授予该工具组" : undefined}
                    className={`border-border/70 flex items-center gap-2 rounded-lg border px-3 py-2 transition-colors ${locked ? "bg-muted/40 text-muted-foreground cursor-not-allowed" : "bg-background hover:bg-muted/50 cursor-pointer"}`}
                  >
                    <input
                      type="checkbox"
                      aria-label={group}
                      checked={selectedToolGroups.includes(group)}
                      disabled={locked}
                      onChange={(event) => {
                        setSelectedToolGroups((current) => event.target.checked
                          ? [...current, group]
                          : current.filter((item) => item !== group));
                      }}
                    />
                    <span className="min-w-0 flex-1 truncate">{group}</span>
                  </label>
                );
              })}
            </div>
          </div>
          <ModelSettingsEditor value={modelSettings} onChange={setModelSettings} />
        </div>
        <div className="flex justify-end gap-2 border-t pt-4"><Button variant="outline" onClick={onCancel}>取消</Button><Button onClick={() => void save()} disabled={saving}>{saving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}保存配置</Button></div>
      </div>
    </section>
  );
}

export function AgentConfigurationPanel({ api = systemAgentConfigurationApi }: { api?: AgentConfigurationApi }) {
  const [agents, setAgents] = useState<AgentConfiguration[] | null>(null);
  const [toolGroups, setToolGroups] = useState<ToolGroupCatalog[] | null>(null);
  const [editing, setEditing] = useState<AgentConfiguration | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [configurationSaved, setConfigurationSaved] = useState(false);

  const load = async () => {
    try {
      setError(null);
      const [nextAgents, { groups }] = await Promise.all([api.list(), getToolGroups()]);
      setAgents(nextAgents);
      setToolGroups(groups);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取 Agent 配置失败");
    }
  };
  useEffect(() => { void load(); }, [api]);
  if (editing !== undefined && toolGroups) return <AgentEditor api={api} toolGroups={toolGroups} initial={editing} onCancel={() => setEditing(undefined)} onSaved={(agent) => { setConfigurationSaved(true); setEditing(undefined); setAgents((current) => current ? (current.some((item) => item.agent_id === agent.agent_id) ? current.map((item) => item.agent_id === agent.agent_id ? agent : item) : [...current, agent]) : [agent]); }} />;
  if (!agents || !toolGroups) return error ? <LoadErrorState message={error} onRetry={() => void load()} /> : <LoadingState />;
  return (
    <div className="space-y-4">
      <ErrorNotice message={error} />
      {configurationSaved && <div role="status" className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-300">配置更改成功</div>}
      <div className="flex items-center justify-end"><Button onClick={() => setEditing(null)}><PlusIcon />新建 Agent</Button></div>
      <div className="grid gap-3 xl:grid-cols-2">
        {agents.map((agent) => (
          <article key={`${agent.source}:${agent.agent_id}`} className="border-border/70 bg-card/70 rounded-2xl border p-4 shadow-sm transition-shadow hover:shadow-md">
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 items-center gap-3">
                <div className="bg-primary/10 text-primary flex size-9 shrink-0 items-center justify-center rounded-xl"><Code2Icon className="size-4" /></div>
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <h3 className="min-w-0 truncate font-medium">{agent.agent_id}</h3>
                    <span className={`shrink-0 whitespace-nowrap rounded-full px-2 py-0.5 text-[10px] ${agent.source === "builtin" ? "bg-muted text-muted-foreground" : "bg-primary/10 text-primary"}`}>
                      {agent.source === "builtin" ? "内置" : "自定义"}
                    </span>
                  </div>
                </div>
              </div>
              {agent.editable || agent.deletable ? (
                <div className="flex gap-1">
                  {agent.editable && <Button variant="ghost" size="icon-sm" onClick={() => setEditing(agent)} aria-label={`编辑 ${agent.agent_id}`}><Settings2Icon /></Button>}
                  {agent.deletable && <Button variant="ghost" size="icon-sm" className="text-destructive" onClick={() => { if (window.confirm(`删除 Agent “${agent.agent_id}”？`)) void api.remove(agent.agent_id).then(() => { setConfigurationSaved(true); setAgents((current) => current?.filter((item) => item.agent_id !== agent.agent_id) ?? null); }).catch((cause) => setError(cause instanceof Error ? cause.message : "删除失败")); }} aria-label={`删除 ${agent.agent_id}`}><Trash2Icon /></Button>}
                </div>
              ) : (
                <span className="text-muted-foreground flex size-7 shrink-0 items-center justify-center" role="img" aria-label="内置 Agent，不可编辑或删除" title="内置 Agent，不可编辑或删除"><ShieldCheckIcon className="size-4" /></span>
              )}
            </div>
            <div className="text-muted-foreground mt-3 flex flex-wrap gap-2 text-xs">
              <span className="bg-muted text-muted-foreground rounded-md px-2 py-1">{agent.allowed_tool_groups.length} 个工具组</span>
              <span className="bg-muted rounded-md px-2 py-1">{agent.model_config_id ? `配置 ${agent.model_config_id}` : "跟随默认模型"}</span>
            </div>
            {agent.validation_status !== "valid" && <p className="text-destructive mt-3 text-xs">{agent.validation_error ?? "配置无效"}</p>}
          </article>
        ))}
      </div>
    </div>
  );
}

function EnvironmentPanel() {
  const [groups, setGroups] = useState<EnvironmentGroup[] | null>(null);
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const [clearSecrets, setClearSecrets] = useState<Set<string>>(new Set());
  const [dirty, setDirty] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = async () => {
    try {
      setError(null);
      const result = await getEnvironmentConfiguration();
      setGroups(result.groups);
      const fields = result.groups.flatMap((group) => group.fields);
      setValues(Object.fromEntries(fields.map((field) => [
        field.name,
        field.secret ? "" : field.type === "boolean" ? Boolean(field.value) : String(field.value ?? ""),
      ])));
      setDirty(new Set());
      setClearSecrets(new Set());
      setSaved(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取环境变量失败");
    }
  };
  useEffect(() => { void load(); }, []);
  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const changes: Record<string, { operation: "replace" | "clear" | "unchanged"; value?: unknown }> = Object.fromEntries([...dirty].map((name) => {
        const field = groups?.flatMap((group) => group.fields).find((item) => item.name === name);
        const value = values[name];
        if (!field) return [name, { operation: "unchanged" }];
        const operation = field.secret
          ? clearSecrets.has(name) && field.clearable ? "clear" : value === "" ? "unchanged" : "replace"
          : field.type === "boolean" ? "replace" : value === "" && field.clearable ? "clear" : "replace";
        return [name, { operation, value: field.type === "boolean" ? value === true || value === "true" : value }];
      }));
      const result = await updateEnvironmentConfiguration(changes);
      setGroups(result.groups);
      setDirty(new Set());
      setClearSecrets(new Set());
      setSaved(true);
      const fields = result.groups.flatMap((group) => group.fields);
      setValues(Object.fromEntries(fields.map((field) => [
        field.name,
        field.secret ? "" : field.type === "boolean" ? Boolean(field.value) : String(field.value ?? ""),
      ])));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存环境变量失败");
    } finally {
      setSaving(false);
    }
  };
  if (!groups) return error ? <LoadErrorState message={error} onRetry={() => void load()} /> : <LoadingState />;
  return (
    <div className="space-y-4">
      <div className="bg-background/95 border-border/70 sticky top-0 z-10 -mx-2 flex flex-wrap items-center justify-between gap-3 border-b px-2 py-3 backdrop-blur-sm">
        <div>
          <h2 className="font-medium">环境变量</h2>
          <p className="text-muted-foreground mt-1 text-xs">管理本机 Agent 的运行环境配置。</p>
        </div>
        <Button onClick={() => void save()} disabled={saving || dirty.size === 0}>
          {saving ? <Loader2Icon className="animate-spin" /> : saved ? <CheckIcon /> : <SaveIcon />}{saved ? "已保存" : "保存变量"}{dirty.size > 0 ? `（${dirty.size}）` : ""}
        </Button>
      </div>
      <ErrorNotice message={error} />
      <EnvironmentConfigurationForm
        groups={groups}
        values={values as Record<string, EnvironmentFieldValue>}
        onChange={(field, value) => {
          setValues((current) => ({ ...current, [field.name]: value }));
          setSaved(false);
          setClearSecrets((current) => {
            const next = new Set(current);
            next.delete(field.name);
            return next;
          });
          setDirty((current) => new Set(current).add(field.name));
        }}
        onToggle={(field) => {
          setValues((current) => ({ ...current, [field.name]: !(current[field.name] === true || current[field.name] === "true") }));
          setSaved(false);
          setDirty((current) => new Set(current).add(field.name));
        }}
        onClear={(field) => {
          setClearSecrets((current) => new Set(current).add(field.name));
          setValues((current) => ({ ...current, [field.name]: "" }));
          setSaved(false);
          setDirty((current) => new Set(current).add(field.name));
        }}
      />
    </div>
  );
}

export function SystemConfigurationPage({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<ConfigurationTab>("agents");
  return <div className="bg-background/95 absolute inset-0 z-30 overflow-y-auto backdrop-blur-sm"><div className="mx-auto flex min-h-full w-full max-w-6xl flex-col px-5 py-6 lg:px-10"><header className="mb-8 flex flex-wrap items-start justify-between gap-4"><div><div className="text-muted-foreground mb-3 flex items-center gap-2 text-xs"><Settings2Icon className="size-3.5" />系统配置中心</div><h1 className="text-2xl font-semibold tracking-tight">让本机 Agent 按你的方式工作</h1></div><Button variant="outline" onClick={onClose}><ChevronLeftIcon />返回工作区</Button></header><div className="grid min-h-0 flex-1 gap-8 lg:grid-cols-[13rem_minmax(0,1fr)]"><nav className="flex gap-2 overflow-x-auto lg:block lg:space-y-2" aria-label="系统配置分类">{tabItems.map((item) => { const Icon = item.icon; return <button key={item.id} type="button" onClick={() => setTab(item.id)} className={`flex min-w-44 items-center gap-3 rounded-xl px-3 py-3 text-left transition-colors lg:w-full ${tab === item.id ? "bg-primary text-primary-foreground shadow-sm" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}><Icon className="size-4 shrink-0" /><span className="min-w-0"><span className="block text-sm font-medium">{item.label}</span><span className={`mt-0.5 block truncate text-[11px] ${tab === item.id ? "text-primary-foreground/70" : "text-muted-foreground"}`}>{item.description}</span></span></button>; })}</nav><main className="min-w-0">{tab === "agents" && <AgentConfigurationPanel />}{tab === "agent-teams" && <AgentTeamConfigurationPanel scope="system" />}{tab === "main-agent" && <MarkdownConfigurationPanel adapter={mainAgentPromptConfigurationAdapter} description="保存后实时生效，不影响前缀缓存。" placeholder="定义主 Agent 的执行协议…" saveLabel="保存主 Agent prompt" initialMode="split" />}{tab === "instructions" && <MarkdownConfigurationPanel adapter={globalInstructionConfigurationAdapter} description="保存后实时生效，不影响前缀缓存。" placeholder="在这里写入系统级工作约束…" saveLabel="保存指令" initialMode="split" />}{tab === "environment" && <EnvironmentPanel />}{tab === "terminal-denylist" && <TerminalDenylistConfigurationPanel />}</main></div></div></div>;
}
