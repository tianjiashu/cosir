import { jsonRequestInit, requestJson, type RequestOptions } from "@/lib/http/client";

export type AgentConfiguration = {
  agent_id: string;
  role: string;
  description: string;
  system_prompt: string;
  allowed_tool_groups: string[];
  max_steps: number;
  model_config_id: number | null;
  model_settings: Record<string, unknown>;
  source: "builtin" | "user_file" | string;
  path: string | null;
  editable: boolean;
  deletable: boolean;
  validation_status: string;
  validation_error: string | null;
  file_name: string | null;
};

export type AgentConfigurationInput = Pick<
  AgentConfiguration,
  | "agent_id"
  | "role"
  | "description"
  | "system_prompt"
  | "allowed_tool_groups"
  | "max_steps"
  | "model_config_id"
  | "model_settings"
>;

export type GlobalInstructionConfiguration = {
  content: string;
  path: string;
  token_length: number;
  max_tokens: number;
  effective_on: "next_run";
};

export type MainAgentPromptConfiguration = {
  content: string;
  path: string;
  token_length: number;
  max_tokens: number;
  source: "user_file" | string;
  effective_on: "next_run";
};

export type WorkspaceInstructionConfiguration = {
  content: string;
  path: string;
  relative_path: string;
  token_length: number;
  max_tokens: number;
  exists: boolean;
  effective_on: "next_run" | string;
};

export type WorkspaceFileIgnoreConfiguration = {
  content: string;
  path: string;
  exists: boolean;
  rule_count: number;
  max_rules: number;
};

export type EnvironmentField = {
  name: string;
  type: "string" | "boolean" | "integer";
  component: "input" | "password" | "checkbox" | "select";
  label: string;
  secret: boolean;
  default: string | number | boolean | null;
  value: string | number | boolean | null;
  disk_value: string | number | boolean | null;
  configured: boolean;
  masked: boolean;
  source: "process" | "file" | "default";
  process_override: boolean;
  options: Array<{ value: string; label: string }>;
  placeholder: string | null;
  clearable: boolean;
  minimum: number | null;
  maximum: number | null;
};

export type EnvironmentGroup = {
  id: string;
  label: string;
  description: string;
  fields: EnvironmentField[];
};

/** 子 Agent 与 Team 配置的作用域：系统级，或某个工作区自己的。 */
export type ConfigurationScope = "system" | "workspace";

/**
 * 子 Agent 与 Team 配置的拥有者统一以 `workspaceId` 表达（null 为系统级，与查询键同一约定）；
 * 路径、请求体里的作用域名都由它推出，调用方不再另传一份。
 */
export function configurationScope(workspaceId: number | null): ConfigurationScope {
  return workspaceId === null ? "system" : "workspace";
}

function agentConfigurationPath(workspaceId: number | null, agentId?: string) {
  const root = workspaceId === null ? "/configuration/agents" : `/workspaces/${workspaceId}/configuration/agents`;
  return agentId === undefined ? root : `${root}/${encodeURIComponent(agentId)}`;
}

/** 某一作用域自己的子 Agent 配置（工作区列表不含继承来的系统级配置）。 */
export function getAgentConfigurations(workspaceId: number | null, options?: RequestOptions): Promise<AgentConfiguration[]> {
  return requestJson<AgentConfiguration[]>(agentConfigurationPath(workspaceId), options);
}

export function createAgentConfiguration(workspaceId: number | null, input: AgentConfigurationInput): Promise<AgentConfiguration> {
  return requestJson<AgentConfiguration>(agentConfigurationPath(workspaceId), jsonRequestInit(input, { method: "POST" }));
}

export function updateAgentConfiguration(workspaceId: number | null, agentId: string, input: AgentConfigurationInput): Promise<AgentConfiguration> {
  return requestJson<AgentConfiguration>(agentConfigurationPath(workspaceId, agentId), jsonRequestInit(input, { method: "PUT" }));
}

export async function deleteAgentConfiguration(workspaceId: number | null, agentId: string): Promise<void> {
  await requestJson(agentConfigurationPath(workspaceId, agentId), { method: "DELETE" });
}

export function getGlobalInstructionConfiguration(options?: RequestOptions): Promise<GlobalInstructionConfiguration> {
  return requestJson<GlobalInstructionConfiguration>("/configuration/global-instructions", options);
}

export function updateGlobalInstructionConfiguration(content: string): Promise<GlobalInstructionConfiguration> {
  return requestJson<GlobalInstructionConfiguration>("/configuration/global-instructions", jsonRequestInit({ content }, { method: "PUT" }));
}

export function getMainAgentPromptConfiguration(options?: RequestOptions): Promise<MainAgentPromptConfiguration> {
  return requestJson<MainAgentPromptConfiguration>("/configuration/main-agent-prompt", options);
}

export function updateMainAgentPromptConfiguration(content: string): Promise<MainAgentPromptConfiguration> {
  return requestJson<MainAgentPromptConfiguration>("/configuration/main-agent-prompt", jsonRequestInit({ content }, { method: "PUT" }));
}

export function getEnvironmentConfiguration(options?: RequestOptions): Promise<{ groups: EnvironmentGroup[] }> {
  return requestJson("/configuration/environment", options);
}

export function updateEnvironmentConfiguration(changes: Record<string, { operation: "replace" | "clear" | "unchanged"; value?: unknown }>): Promise<{ groups: EnvironmentGroup[] }> {
  return requestJson("/configuration/environment", jsonRequestInit({ changes }, { method: "PUT" }));
}

export function getWorkspaceInstructionConfiguration(workspaceId: number, options?: RequestOptions): Promise<WorkspaceInstructionConfiguration> {
  return requestJson<WorkspaceInstructionConfiguration>(`/workspaces/${workspaceId}/configuration/instructions`, options);
}

export function updateWorkspaceInstructionConfiguration(workspaceId: number, content: string): Promise<WorkspaceInstructionConfiguration> {
  return requestJson<WorkspaceInstructionConfiguration>(`/workspaces/${workspaceId}/configuration/instructions`, jsonRequestInit({ content }, { method: "PUT" }));
}

export function getWorkspaceFileIgnoreConfiguration(workspaceId: number, options?: RequestOptions): Promise<WorkspaceFileIgnoreConfiguration> {
  return requestJson<WorkspaceFileIgnoreConfiguration>(`/workspaces/${workspaceId}/configuration/fileignore`, options);
}

export function updateWorkspaceFileIgnoreConfiguration(workspaceId: number, content: string): Promise<WorkspaceFileIgnoreConfiguration> {
  return requestJson<WorkspaceFileIgnoreConfiguration>(`/workspaces/${workspaceId}/configuration/fileignore`, jsonRequestInit({ content }, { method: "PUT" }));
}
