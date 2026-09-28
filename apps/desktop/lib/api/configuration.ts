import { jsonRequestInit, requestJson } from "@/lib/http/client";

export type AgentConfiguration = {
  agent_id: string;
  role: string;
  description: string;
  system_prompt: string;
  allowed_tool_groups: string[];
  max_steps: number;
  provider_id: number | null;
  model_name: string | null;
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
  | "provider_id"
  | "model_name"
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
  source: "user_file" | "builtin_default" | "builtin_fallback" | string;
  effective_on: "next_run";
};

export type EnvironmentField = {
  name: string;
  type: "string" | "boolean";
  component: "input" | "password" | "checkbox" | "select";
  label: string;
  secret: boolean;
  default: string | boolean | null;
  value: string | boolean | null;
  disk_value: string | boolean | null;
  configured: boolean;
  masked: boolean;
  source: "process" | "file" | "default";
  process_override: boolean;
  options: Array<{ value: string; label: string }>;
  placeholder: string | null;
  clearable: boolean;
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
