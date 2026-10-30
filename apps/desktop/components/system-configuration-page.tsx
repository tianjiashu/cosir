"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useStore } from "zustand";
import {
  ChevronLeftIcon,
  Code2Icon,
  Loader2Icon,
  PlusIcon,
  SaveIcon,
  Settings2Icon,
  ShieldCheckIcon,
  Trash2Icon,
} from "lucide-react";

import { SettingsPageLayout } from "@/components/settings/settings-page-layout";
import { AppearanceSettingsPanel } from "@/components/settings/appearance-settings-panel";
import { SYSTEM_SETTINGS_TABS, type SystemSettingsTabId } from "@/components/settings/settings-tabs";
import { EnvironmentConfigurationForm, type EnvironmentFieldValue } from "@/components/environment-configuration-form";
import { MarkdownConfigurationPanel } from "@/components/configuration/markdown-configuration-panel";
import { AgentTeamConfigurationPanel } from "@/components/agent-team-configuration-panel";
import { TerminalDenylistConfigurationPanel } from "@/components/terminal-denylist-configuration-panel";
import { MCPConfigurationPanel } from "@/components/configuration/mcp-configuration-panel";
import { SkillConfigurationPanel } from "@/components/configuration/skill-configuration-panel";
import { AgentConfigurationForm } from "@/components/configuration/agent-configuration-form";
import { ModelConfigManager } from "@/components/model/model-config-manager";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { getToolGroups, type ToolGroupCatalog } from "@/lib/api/tools";
import {
  modelOptionId,
  unknownModelName,
  useModelCatalog,
  type ModelCatalog,
} from "@/lib/model-catalog";
import {
  createAgentConfiguration,
  deleteAgentConfiguration,
  getAgentConfigurations,
  getEnvironmentConfiguration,
  getGlobalInstructionConfiguration,
  getMainAgentPromptConfiguration,
  type AgentConfiguration,
  type AgentConfigurationInput,
  type EnvironmentField,
  type EnvironmentGroup,
  type GlobalInstructionConfiguration,
  type MainAgentPromptConfiguration,
  updateAgentConfiguration,
  updateEnvironmentConfiguration,
  updateGlobalInstructionConfiguration,
  updateMainAgentPromptConfiguration,
} from "@/lib/api/configuration";
import type { MarkdownConfigurationAdapter } from "@/hooks/use-markdown-configuration-document";
import { createAgentEditorSession } from "@/lib/agent-editor/session";
import { confirmDelete } from "@/lib/confirm/confirm-store";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import type { RequestOptions } from "@/lib/http/client";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { confirmDiscardUnsaved, useUnsavedChanges } from "@/lib/unsaved-changes";


/**
 * 子 Agent 配置的作用域适配：系统级与工作区级共用同一套列表 / 编辑界面。
 * `workspaceId` 决定查询键（null 为系统级），保存与删除后按它失效对应列表。
 */
export type AgentConfigurationApi = {
  workspaceId: number | null;
  list: (options?: RequestOptions) => Promise<AgentConfiguration[]>;
  create: (input: AgentConfigurationInput) => Promise<AgentConfiguration>;
  update: (agentId: string, input: AgentConfigurationInput) => Promise<AgentConfiguration>;
  remove: (agentId: string) => Promise<void>;
};

const systemAgentConfigurationApi: AgentConfigurationApi = {
  workspaceId: null,
  list: getAgentConfigurations,
  create: createAgentConfiguration,
  update: updateAgentConfiguration,
  remove: deleteAgentConfiguration,
};

const globalInstructionConfigurationAdapter: MarkdownConfigurationAdapter<GlobalInstructionConfiguration> = {
  queryKey: queryKeys.instruction("global"),
  label: "全局 AGENTS.md",
  load: getGlobalInstructionConfiguration,
  save: updateGlobalInstructionConfiguration,
  allowEmpty: true,
};

const mainAgentPromptConfigurationAdapter: MarkdownConfigurationAdapter<MainAgentPromptConfiguration> = {
  queryKey: queryKeys.instruction("main-agent-prompt"),
  label: "主 Agent 提示词",
  load: getMainAgentPromptConfiguration,
  save: updateMainAgentPromptConfiguration,
  allowEmpty: false,
};


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
  api,
  onClose,
}: {
  initial: AgentConfiguration | null;
  toolGroups: ToolGroupCatalog[];
  api: AgentConfigurationApi;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [session] = useState(() => createAgentEditorSession({
    initial, saved: initial !== null, scope: api.workspaceId === null ? "system" : "workspace",
  }));
  const dirty = useStore(session.store, (state) => state.dirty);
  const saving = useStore(session.store, (state) => state.saving);
  useUnsavedChanges("settings", initial ? `子 Agent「${initial.agent_id}」` : "新建子 Agent", dirty);

  const mutation = useMutation({
    mutationFn: () => session.save((input, { agentId }) => (
      agentId ? api.update(agentId, input) : api.create(input)
    )),
    onSuccess: async (saved) => {
      if (!saved) return;
      // 等列表刷新后再关闭编辑器，避免返回时短暂显示旧列表。
      await queryClient.invalidateQueries({ queryKey: queryKeys.agents(api.workspaceId) });
      notify.success("配置更改成功");
      onClose();
    },
    onError: (error) => {
      if (error instanceof UserFacingError) return;
      void frontendLog("ERROR", "agent_configuration_save_failed", "保存子 Agent 配置失败", {
        data: { agentId: session.store.getState().form.agent_id, workspaceId: api.workspaceId, isNew: !initial },
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

export function AgentConfigurationPanel({ api = systemAgentConfigurationApi }: { api?: AgentConfigurationApi }) {
  const queryClient = useQueryClient();
  const agentsQuery = useQuery({
    queryKey: queryKeys.agents(api.workspaceId),
    queryFn: ({ signal }) => api.list({ signal }),
  });
  const toolGroupsQuery = useQuery({
    queryKey: queryKeys.toolGroups(),
    queryFn: ({ signal }) => getToolGroups({ signal }),
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
          await api.remove(agent.agent_id);
        } catch (error) {
          void frontendLog("ERROR", "agent_configuration_delete_failed", "删除子 Agent 配置失败", {
            data: { agentId: agent.agent_id, workspaceId: api.workspaceId },
            error,
          });
          throw error;
        }
        await queryClient.invalidateQueries({ queryKey: queryKeys.agents(api.workspaceId) });
      },
    });
    if (removed) notify.success(`已删除子 Agent「${agent.agent_id}」`);
  };

  if (editing !== undefined && toolGroups) return <AgentEditor api={api} toolGroups={toolGroups} initial={editing} onClose={() => setEditing(undefined)} />;
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

type EnvironmentChanges = Parameters<typeof updateEnvironmentConfiguration>[0];

/** 服务端字段值到表单值：密钥永不回显（留空表示不变）。 */
function environmentFormValues(groups: EnvironmentGroup[]): Record<string, string | boolean> {
  return Object.fromEntries(groups.flatMap((group) => group.fields).map((field) => [
    field.name,
    field.secret ? "" : field.type === "boolean" ? Boolean(field.value) : String(field.value ?? ""),
  ]));
}

function environmentChange(field: EnvironmentField, value: string | boolean, clearSecret: boolean): EnvironmentChanges[string] {
  const operation = field.secret
    ? clearSecret && field.clearable ? "clear" : value === "" ? "unchanged" : "replace"
    : field.type === "boolean" ? "replace" : value === "" && field.clearable ? "clear" : "replace";
  return { operation, value: field.type === "boolean" ? value === true || value === "true" : value };
}

function EnvironmentPanel() {
  const query = useQuery({
    queryKey: queryKeys.environment(),
    queryFn: ({ signal }) => getEnvironmentConfiguration({ signal }),
  });
  if (!query.data) {
    return query.isError
      ? <ErrorState className="my-10" message={userErrorMessage(query.error, "读取环境变量失败")} onRetry={() => void query.refetch()} retryLabel="重新读取" />
      : <LoadingState label="正在读取系统配置…" />;
  }
  return <EnvironmentEditor groups={query.data.groups} />;
}

/**
 * 环境变量编辑副本：只记录用户改动过的字段（edits），其余字段跟随服务端，
 * 因此后台重新读取不会覆盖未保存的改动；保存时只提交改动字段。
 */
function EnvironmentEditor({ groups }: { groups: EnvironmentGroup[] }) {
  const queryClient = useQueryClient();
  const [edits, setEdits] = useState<Record<string, string | boolean>>({});
  const [clearSecrets, setClearSecrets] = useState<Set<string>>(() => new Set());
  const serverValues = useMemo(() => environmentFormValues(groups), [groups]);
  const values = { ...serverValues, ...edits };
  const dirtyCount = Object.keys(edits).length;
  useUnsavedChanges("settings", "环境变量", dirtyCount > 0);

  const mutation = useMutation({
    mutationFn: updateEnvironmentConfiguration,
    onSuccess: (result) => {
      queryClient.setQueryData(queryKeys.environment(), result);
      setEdits({});
      setClearSecrets(new Set());
      notify.success("环境变量已保存");
    },
    onError: (error, changes) => {
      void frontendLog("ERROR", "environment_configuration_save_failed", "保存环境变量失败", {
        data: { fields: Object.keys(changes) },
        error,
      });
    },
  });

  const save = () => {
    const fields = new Map(groups.flatMap((group) => group.fields).map((field) => [field.name, field]));
    const changes: EnvironmentChanges = Object.fromEntries(Object.entries(edits).map(([name, value]) => {
      const field = fields.get(name);
      // 后端换代后字段可能已下线：不提交未知字段的值。
      return [name, field ? environmentChange(field, value, clearSecrets.has(name)) : { operation: "unchanged" }];
    }));
    mutation.mutate(changes);
  };

  const edit = (field: EnvironmentField, value: string | boolean) => {
    setEdits((current) => ({ ...current, [field.name]: value }));
    if (mutation.isError) mutation.reset();
  };

  return (
    <div className="space-y-4">
      <div className="bg-background/95 border-border/70 sticky top-0 z-10 -mx-2 flex flex-wrap items-center justify-between gap-3 border-b px-2 py-3 backdrop-blur-sm">
        <div>
          <h2 className="font-medium">环境变量</h2>
          <p className="text-muted-foreground mt-1 text-xs">管理本机 Agent 的运行环境配置。</p>
        </div>
        <Button onClick={save} disabled={mutation.isPending || dirtyCount === 0}>
          {mutation.isPending ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}保存变量{dirtyCount > 0 ? `（${dirtyCount}）` : ""}
        </Button>
      </div>
      {mutation.isError && <Alert tone="destructive">{userErrorMessage(mutation.error, "保存环境变量失败")}</Alert>}
      {/* 保存期间锁定表单，避免请求进行中的改动在成功后被一并清空。 */}
      <fieldset disabled={mutation.isPending} className="m-0 min-w-0 border-0 p-0">
        <EnvironmentConfigurationForm
          groups={groups}
          values={values as Record<string, EnvironmentFieldValue>}
          onChange={(field, value) => {
            edit(field, value);
            setClearSecrets((current) => {
              const next = new Set(current);
              next.delete(field.name);
              return next;
            });
          }}
          onToggle={(field) => edit(field, !(values[field.name] === true || values[field.name] === "true"))}
          onClear={(field) => {
            edit(field, "");
            setClearSecrets((current) => new Set(current).add(field.name));
          }}
          pendingClears={clearSecrets}
          onUndoClear={(field) => {
            // 撤销即回到「未改动」：从改动集合中移除，保存时不再提交该字段。
            setEdits((current) => {
              const next = { ...current };
              delete next[field.name];
              return next;
            });
            setClearSecrets((current) => {
              const next = new Set(current);
              next.delete(field.name);
              return next;
            });
          }}
        />
      </fieldset>
    </div>
  );
}

/**
 * 配置中心的「模型」标签：与 composer 模型设置对话框共用 ModelConfigManager。
 * 这里不在下拉之内，保存 / 删除后立即强制刷新模型选择目录，让各处选择器（含保活的任务页）及时看到变化。
 */
function ModelsPanel() {
  return (
    <div className="space-y-4">
      <div>
        <h2 className="font-medium">模型</h2>
        <p className="text-muted-foreground mt-1 text-xs">管理 OpenAI 兼容接口的连接配置，测试连接通过后才能保存。</p>
      </div>
      <ModelConfigManager unsavedScope="settings" variant="page" />
    </div>
  );
}

/**
 * 系统配置中心（/settings/:tab）。标签来自路由，布局与导航由 SettingsPageLayout 统一提供。
 */
export function SystemConfigurationPage({ tab }: { tab: SystemSettingsTabId }) {
  return (
    <SettingsPageLayout tabs={SYSTEM_SETTINGS_TABS} basePath="/settings" navLabel="系统配置分类">
      {tab === "appearance" && <AppearanceSettingsPanel />}
      {tab === "agents" && <AgentConfigurationPanel />}
      {tab === "models" && <ModelsPanel />}
      {tab === "agent-teams" && <AgentTeamConfigurationPanel scope="system" />}
      {tab === "main-agent" && <MarkdownConfigurationPanel adapter={mainAgentPromptConfigurationAdapter} description="保存后实时生效，不影响前缀缓存。" placeholder="定义主 Agent 的执行协议…" saveLabel="保存主 Agent 提示词" />}
      {tab === "instructions" && <MarkdownConfigurationPanel adapter={globalInstructionConfigurationAdapter} description="保存后实时生效，不影响前缀缓存。" placeholder="在这里写入系统级工作约束…" saveLabel="保存全局 AGENTS.md" />}
      {tab === "environment" && <EnvironmentPanel />}
      {tab === "terminal-denylist" && <TerminalDenylistConfigurationPanel />}
      {tab === "mcp" && <MCPConfigurationPanel />}
      {tab === "skills" && <SkillConfigurationPanel />}
    </SettingsPageLayout>
  );
}
