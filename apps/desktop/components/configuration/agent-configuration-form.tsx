"use client";

import { useMemo } from "react";
import { Link } from "react-router-dom";
import { useStore } from "zustand";

import { MarkdownSourcePreviewEditor } from "@/components/configuration/markdown-source-preview-editor";
import { ModelAvailability } from "@/components/model/model-availability";
import {
  DEFAULT_EFFORT_OPTIONS,
  ModelSelectorContent,
  ModelSelectorList,
  ModelSelectorRoot,
  ModelSelectorSearch,
  ModelSelectorTrigger,
  ModelSelectorValue,
  type ModelOption,
} from "@/components/model-selector";
import { buttonVariants } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";
import type { AgentConfigurationScope, AgentEditorSession } from "@/lib/agent-editor/session";
import type { ToolGroupCatalog } from "@/lib/api/tools";
import { modelOptionId, unknownModelName, useModelCatalog, type ModelCatalogModel } from "@/lib/model-catalog";
import type { ModelSettingsForm } from "@/lib/model-settings-form";

/** 子 Agent 仍不可授予交互终端会话和配置生成能力。 */
const NON_ASSIGNABLE_TOOL_GROUPS = new Set(["交互终端工具", "配置生成工具"]);

const NO_AGENT_MODEL = "__agent_model_default__";

function toAgentModelOption(model: ModelCatalogModel): ModelOption {
  return {
    id: model.optionId,
    name: model.label,
    keywords: [model.configName, model.modelName],
  };
}

function AgentModelSelector({
  modelConfigId,
  onChange,
}: {
  modelConfigId: number | null;
  onChange: (modelConfigId: number | null) => void;
}) {
  const { catalog, status, retry } = useModelCatalog();
  const modelOptions = useMemo(() => {
    const options: ModelOption[] = [
      { id: NO_AGENT_MODEL, name: "默认", description: "继承主 Agent" },
      ...(catalog?.models.map(toAgentModelOption) ?? []),
    ];
    if (modelConfigId === null) return options;
    const currentModelId = modelOptionId(modelConfigId);
    if (options.some((option) => option.id === currentModelId)) return options;
    return [
      ...options,
      { id: currentModelId, name: unknownModelName(modelConfigId), description: "当前配置（目录中不可用）" },
    ];
  }, [catalog?.models, modelConfigId]);

  const selectedId = modelConfigId !== null ? modelOptionId(modelConfigId) : NO_AGENT_MODEL;
  return (
    <ModelAvailability
      variant="field"
      status={status}
      modelCount={catalog?.models.length ?? 0}
      onRetry={retry}
      configure={<Link to="/settings/models" className={buttonVariants({ variant: "link", size: "sm" })}>配置模型</Link>}
    >
      <ModelSelectorRoot
        models={modelOptions}
        value={selectedId}
        onValueChange={(nextId) => {
          if (nextId === NO_AGENT_MODEL) {
            onChange(null);
            return;
          }
          const selected = catalog?.models.find((model) => model.optionId === nextId);
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
    </ModelAvailability>
  );
}

function ModelSettingsEditor({
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
          <NativeSelect value={value.reasoning_effort} onChange={(event) => setField("reasoning_effort", event.target.value)}>
            <option value="">跟随默认</option>
            {DEFAULT_EFFORT_OPTIONS.map((option) => <option key={option.id} value={option.id}>{option.name}</option>)}
          </NativeSelect>
        </label>
      </div>
    </div>
  );
}


/**
 * 子 Agent 配置的字段表单：配置页编辑器与工作台草稿共用。只渲染与提交编辑意图，
 * working copy、未保存判定与保存都归 `AgentEditorSession`；保存后 Agent ID 与保存范围锁定。
 * 布局按容器宽度响应，窄工作台与宽配置页使用同一份表单。
 */
export function AgentConfigurationForm({
  session,
  toolGroups,
  scopeEditable = false,
}: {
  session: AgentEditorSession;
  toolGroups: ToolGroupCatalog[];
  /** 工作台草稿可选择保存到工作区或系统级；配置页的范围由所在页面决定。 */
  scopeEditable?: boolean;
}) {
  const form = useStore(session.store, (state) => state.form);
  const scope = useStore(session.store, (state) => state.scope);
  const identityLocked = useStore(session.store, (state) => state.savedAgentId !== null);
  const saving = useStore(session.store, (state) => state.saving);
  const setField = session.setField;
  return (
    <div className="@container space-y-5">
      {scopeEditable && (
        <label className="block space-y-1.5 text-sm"><span className="text-muted-foreground">保存范围</span><NativeSelect value={scope} disabled={identityLocked || saving} onChange={(event) => session.setScope(event.target.value as AgentConfigurationScope)}><option value="workspace">当前工作区</option><option value="system">系统级</option></NativeSelect></label>
      )}
      <div className="grid gap-4 @md:grid-cols-2">
        <label className="space-y-1.5 text-sm"><span className="text-muted-foreground">Agent ID</span><Input value={form.agent_id} disabled={identityLocked} onChange={(event) => setField("agent_id", event.target.value)} placeholder="主 Agent 调用时使用的 Agent ID" /></label>
        <label className="space-y-1.5 text-sm"><span className="text-muted-foreground">角色</span><Input value={form.role} onChange={(event) => setField("role", event.target.value)} placeholder="子 Agent 的身份定义" /></label>
      </div>
      <label className="block space-y-1.5 text-sm"><span className="text-muted-foreground">描述</span><Textarea className="h-24 min-h-24 max-h-24 resize-none overflow-y-auto" value={form.description} onChange={(event) => setField("description", event.target.value)} placeholder="向主 Agent 说明这个子 Agent 负责什么" /></label>
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
      <div className="grid gap-4 @md:grid-cols-3">
        <label className="space-y-1.5 text-sm"><span className="text-muted-foreground">最大步数</span><Input type="number" min={1} max={10000} value={form.max_steps} onChange={(event) => setField("max_steps", event.target.value)} /></label>
        <div className="space-y-1.5 text-sm @md:col-span-2"><span className="text-muted-foreground">模型 <span className="text-muted-foreground/60">（可选）</span></span><AgentModelSelector modelConfigId={form.model_config_id} onChange={(modelConfigId) => setField("model_config_id", modelConfigId)} /></div>
      </div>
      <div className="grid gap-4 @3xl:grid-cols-2">
        <div className="space-y-1.5 text-sm">
          <span className="text-muted-foreground">允许的工具组</span>
          <div className="border-border/70 bg-muted/20 grid gap-2 rounded-xl border p-3 @md:grid-cols-2">
            {toolGroups.map(({ group }) => {
              const locked = NON_ASSIGNABLE_TOOL_GROUPS.has(group);
              return (
                <label
                  key={group}
                  title={locked ? "子 Agent 不可授予该工具组" : undefined}
                  className={`border-border/70 flex items-center gap-2 rounded-lg border px-3 py-2 transition-colors ${locked ? "bg-muted/40 text-muted-foreground cursor-not-allowed" : "bg-background hover:bg-muted/50 cursor-pointer"}`}
                >
                  <Checkbox
                    aria-label={group}
                    checked={form.allowed_tool_groups.includes(group)}
                    disabled={locked}
                    onChange={(event) => setField("allowed_tool_groups", event.target.checked
                      ? [...form.allowed_tool_groups, group]
                      : form.allowed_tool_groups.filter((item) => item !== group))}
                  />
                  <span className="min-w-0 flex-1 truncate">{group}</span>
                </label>
              );
            })}
          </div>
        </div>
        <ModelSettingsEditor value={form.model_settings} onChange={(value) => setField("model_settings", value)} />
      </div>
    </div>
  );
}
