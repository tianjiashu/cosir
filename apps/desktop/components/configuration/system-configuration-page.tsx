import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2Icon, SaveIcon } from "lucide-react";

import { AgentConfigurationPanel } from "@/components/configuration/agent-configuration-panel";
import { SettingsPageLayout } from "@/components/settings/settings-page-layout";
import { AppearanceSettingsPanel } from "@/components/settings/appearance-settings-panel";
import { SYSTEM_SETTINGS_TABS, type SystemSettingsTabId } from "@/components/settings/settings-tabs";
import { EnvironmentConfigurationForm } from "@/components/configuration/environment-configuration-form";
import { MarkdownConfigurationPanel } from "@/components/configuration/markdown-configuration-panel";
import { AgentTeamConfigurationPanel } from "@/components/agent-team/configuration-panel";
import { MCPConfigurationPanel } from "@/components/configuration/mcp-configuration-panel";
import { SkillConfigurationPanel } from "@/components/configuration/skill-configuration-panel";
import { ModelConfigManager } from "@/components/model/model-config-manager";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import {
  getEnvironmentConfiguration,
  getGlobalInstructionConfiguration,
  getMainAgentPromptConfiguration,
  type EnvironmentField,
  type EnvironmentGroup,
  updateEnvironmentConfiguration,
  updateGlobalInstructionConfiguration,
  updateMainAgentPromptConfiguration,
} from "@/lib/api/configuration";
import type { ConfigurationDocumentAdapter } from "@/components/configuration/use-configuration-document";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import type { MarkdownDocumentContract } from "@/lib/configuration-document";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { useUnsavedChanges } from "@/lib/unsaved-changes";


const globalInstructionConfigurationAdapter: ConfigurationDocumentAdapter<MarkdownDocumentContract> = {
  queryKey: queryKeys.instruction("global"),
  label: "全局 AGENTS.md",
  load: getGlobalInstructionConfiguration,
  save: updateGlobalInstructionConfiguration,
  saveFailedEvent: "markdown_configuration_save_failed",
};

const mainAgentPromptConfigurationAdapter: ConfigurationDocumentAdapter<MarkdownDocumentContract> = {
  queryKey: queryKeys.instruction("main-agent-prompt"),
  label: "主 Agent 提示词",
  load: getMainAgentPromptConfiguration,
  save: updateMainAgentPromptConfiguration,
  saveFailedEvent: "markdown_configuration_save_failed",
};


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
          values={values}
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
 * 保存 / 删除后的模型目录刷新由 ModelConfigManager 失效共享查询缓存完成，这里不另做。
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
      {tab === "agents" && <AgentConfigurationPanel workspaceId={null} />}
      {tab === "models" && <ModelsPanel />}
      {tab === "agent-teams" && <AgentTeamConfigurationPanel workspaceId={null} />}
      {tab === "main-agent" && <MarkdownConfigurationPanel adapter={mainAgentPromptConfigurationAdapter} requireContent description="保存后实时生效，不影响前缀缓存。" placeholder="定义主 Agent 的执行协议…" saveLabel="保存主 Agent 提示词" />}
      {tab === "instructions" && <MarkdownConfigurationPanel adapter={globalInstructionConfigurationAdapter} description="保存后实时生效，不影响前缀缓存。" placeholder="在这里写入系统级工作约束…" saveLabel="保存全局 AGENTS.md" />}
      {tab === "environment" && <EnvironmentPanel />}
      {tab === "mcp" && <MCPConfigurationPanel workspaceId={null} />}
      {tab === "skills" && <SkillConfigurationPanel workspaceId={null} />}
    </SettingsPageLayout>
  );
}
