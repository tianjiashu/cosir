import { jsonRequestInit, requestJson } from "@/lib/http/client";

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

export type TerminalDenylistConfiguration = {
  patterns: string[];
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

export async function getAgentConfigurations(): Promise<AgentConfiguration[]> {
  return requestJson<AgentConfiguration[]>("/configuration/agents");
}

export async function createAgentConfiguration(input: AgentConfigurationInput): Promise<AgentConfiguration> {
  return requestJson<AgentConfiguration>("/configuration/agents", jsonRequestInit(input, { method: "POST" }));
}

export async function updateAgentConfiguration(agentId: string, input: AgentConfigurationInput): Promise<AgentConfiguration> {
  return requestJson<AgentConfiguration>(`/configuration/agents/${encodeURIComponent(agentId)}`, jsonRequestInit(input, { method: "PUT" }));
}

export async function deleteAgentConfiguration(agentId: string): Promise<void> {
  await requestJson(`/configuration/agents/${encodeURIComponent(agentId)}`, { method: "DELETE" });
}

export function getGlobalInstructionConfiguration(): Promise<GlobalInstructionConfiguration> {
  return requestJson<GlobalInstructionConfiguration>("/configuration/global-instructions");
}

export function updateGlobalInstructionConfiguration(content: string): Promise<GlobalInstructionConfiguration> {
  return requestJson<GlobalInstructionConfiguration>("/configuration/global-instructions", jsonRequestInit({ content }, { method: "PUT" }));
}

export function getMainAgentPromptConfiguration(): Promise<MainAgentPromptConfiguration> {
  return requestJson<MainAgentPromptConfiguration>("/configuration/main-agent-prompt");
}

export function updateMainAgentPromptConfiguration(content: string): Promise<MainAgentPromptConfiguration> {
  return requestJson<MainAgentPromptConfiguration>("/configuration/main-agent-prompt", jsonRequestInit({ content }, { method: "PUT" }));
}

export function getEnvironmentConfiguration(): Promise<{ groups: EnvironmentGroup[] }> {
  return requestJson("/configuration/environment");
}

export function updateEnvironmentConfiguration(changes: Record<string, { operation: "replace" | "clear" | "unchanged"; value?: unknown }>): Promise<{ groups: EnvironmentGroup[] }> {
  return requestJson("/configuration/environment", jsonRequestInit({ changes }, { method: "PUT" }));
}

export function getTerminalDenylistConfiguration(): Promise<TerminalDenylistConfiguration> {
  return requestJson<TerminalDenylistConfiguration>("/configuration/terminal-denylist");
}

export function updateTerminalDenylistConfiguration(patterns: string[]): Promise<TerminalDenylistConfiguration> {
  return requestJson<TerminalDenylistConfiguration>(
    "/configuration/terminal-denylist",
    jsonRequestInit({ patterns }, { method: "PUT" }),
  );
}

export function getWorkspaceAgentConfigurations(workspaceId: number): Promise<AgentConfiguration[]> {
  return requestJson<AgentConfiguration[]>(`/workspaces/${workspaceId}/configuration/agents`);
}

export function createWorkspaceAgentConfiguration(workspaceId: number, input: AgentConfigurationInput): Promise<AgentConfiguration> {
  return requestJson<AgentConfiguration>(`/workspaces/${workspaceId}/configuration/agents`, jsonRequestInit(input, { method: "POST" }));
}

export function updateWorkspaceAgentConfiguration(workspaceId: number, agentId: string, input: AgentConfigurationInput): Promise<AgentConfiguration> {
  return requestJson<AgentConfiguration>(`/workspaces/${workspaceId}/configuration/agents/${encodeURIComponent(agentId)}`, jsonRequestInit(input, { method: "PUT" }));
}

export function deleteWorkspaceAgentConfiguration(workspaceId: number, agentId: string): Promise<void> {
  return requestJson(`/workspaces/${workspaceId}/configuration/agents/${encodeURIComponent(agentId)}`, { method: "DELETE" }).then(() => undefined);
}

export function getWorkspaceInstructionConfiguration(workspaceId: number): Promise<WorkspaceInstructionConfiguration> {
  return requestJson<WorkspaceInstructionConfiguration>(`/workspaces/${workspaceId}/configuration/instructions`);
}

export function updateWorkspaceInstructionConfiguration(workspaceId: number, content: string): Promise<WorkspaceInstructionConfiguration> {
  return requestJson<WorkspaceInstructionConfiguration>(`/workspaces/${workspaceId}/configuration/instructions`, jsonRequestInit({ content }, { method: "PUT" }));
}

export function getWorkspaceFileIgnoreConfiguration(workspaceId: number): Promise<WorkspaceFileIgnoreConfiguration> {
  return requestJson<WorkspaceFileIgnoreConfiguration>(`/workspaces/${workspaceId}/configuration/fileignore`);
}

export function updateWorkspaceFileIgnoreConfiguration(workspaceId: number, content: string): Promise<WorkspaceFileIgnoreConfiguration> {
  return requestJson<WorkspaceFileIgnoreConfiguration>(`/workspaces/${workspaceId}/configuration/fileignore`, jsonRequestInit({ content }, { method: "PUT" }));
}
