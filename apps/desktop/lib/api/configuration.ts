import { configurationApiPath } from "@/lib/api/configuration-scope";
import { jsonRequestInit, requestJson, type RequestOptions } from "@/lib/http/client";

export type AgentConfiguration = {
  agent_id: string;
  role: string;
  description: string;
  system_prompt: string;
  allowed_tool_groups: string[];
  max_steps: number;
  model_config_id: string | null;
  model_settings: Record<string, unknown>;
  source: "builtin" | "user_file" | string;
  deletable: boolean;
  validation_status: string;
  validation_error: string | null;
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

/**
 * 指令类 Markdown 文档（全局 / 工作区 AGENTS.md、主 Agent 提示词）。后端还返回路径、来源等字段，前端只用
 * 正文与 Token 预算；.fileignore 只用正文（规则上限由后端校验）。
 */
export type InstructionDocument = { content: string; max_tokens: number };
export type FileIgnoreDocument = { content: string };

export type EnvironmentField = {
  name: string;
  type: "string" | "boolean" | "integer";
  component: "input" | "password" | "checkbox" | "select";
  label: string;
  secret: boolean;
  value: string | number | boolean | null;
  masked: boolean;
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

function agentConfigurationPath(workspaceId: number | null, agentId?: string) {
  return configurationApiPath(workspaceId, agentId === undefined ? "/agents" : `/agents/${encodeURIComponent(agentId)}`);
}

/** 某一作用域自己的子 Agent 配置（工作区列表不含继承来的系统级配置）。 */
export function getAgentConfigurations(workspaceId: number | null, options?: RequestOptions): Promise<AgentConfiguration[]> {
  return requestJson<AgentConfiguration[]>(agentConfigurationPath(workspaceId), options);
}

function createAgentConfiguration(workspaceId: number | null, input: AgentConfigurationInput): Promise<AgentConfiguration> {
  return requestJson<AgentConfiguration>(agentConfigurationPath(workspaceId), jsonRequestInit(input, { method: "POST" }));
}

function updateAgentConfiguration(workspaceId: number | null, agentId: string, input: AgentConfigurationInput): Promise<AgentConfiguration> {
  return requestJson<AgentConfiguration>(agentConfigurationPath(workspaceId, agentId), jsonRequestInit(input, { method: "PUT" }));
}

/** 保存一份子 Agent 配置：已保存过（有 `agentId`）就更新它，否则在该作用域新建。 */
export function saveAgentConfiguration(workspaceId: number | null, agentId: string | null, input: AgentConfigurationInput): Promise<AgentConfiguration> {
  return agentId ? updateAgentConfiguration(workspaceId, agentId, input) : createAgentConfiguration(workspaceId, input);
}

export async function deleteAgentConfiguration(workspaceId: number | null, agentId: string): Promise<void> {
  await requestJson(agentConfigurationPath(workspaceId, agentId), { method: "DELETE" });
}

export function getGlobalInstructionConfiguration(options?: RequestOptions): Promise<InstructionDocument> {
  return requestJson<InstructionDocument>("/configuration/global-instructions", options);
}

export function updateGlobalInstructionConfiguration(content: string): Promise<InstructionDocument> {
  return requestJson<InstructionDocument>("/configuration/global-instructions", jsonRequestInit({ content }, { method: "PUT" }));
}

export function getMainAgentPromptConfiguration(options?: RequestOptions): Promise<InstructionDocument> {
  return requestJson<InstructionDocument>("/configuration/main-agent-prompt", options);
}

export function updateMainAgentPromptConfiguration(content: string): Promise<InstructionDocument> {
  return requestJson<InstructionDocument>("/configuration/main-agent-prompt", jsonRequestInit({ content }, { method: "PUT" }));
}

export function getEnvironmentConfiguration(options?: RequestOptions): Promise<{ groups: EnvironmentGroup[] }> {
  return requestJson("/configuration/environment", options);
}

export function updateEnvironmentConfiguration(changes: Record<string, { operation: "replace" | "clear" | "unchanged"; value?: unknown }>): Promise<{ groups: EnvironmentGroup[] }> {
  return requestJson("/configuration/environment", jsonRequestInit({ changes }, { method: "PUT" }));
}

export function getWorkspaceInstructionConfiguration(workspaceId: number, options?: RequestOptions): Promise<InstructionDocument> {
  return requestJson<InstructionDocument>(configurationApiPath(workspaceId, "/instructions"), options);
}

export function updateWorkspaceInstructionConfiguration(workspaceId: number, content: string): Promise<InstructionDocument> {
  return requestJson<InstructionDocument>(configurationApiPath(workspaceId, "/instructions"), jsonRequestInit({ content }, { method: "PUT" }));
}

export function getWorkspaceFileIgnoreConfiguration(workspaceId: number, options?: RequestOptions): Promise<FileIgnoreDocument> {
  return requestJson<FileIgnoreDocument>(configurationApiPath(workspaceId, "/fileignore"), options);
}

export function updateWorkspaceFileIgnoreConfiguration(workspaceId: number, content: string): Promise<FileIgnoreDocument> {
  return requestJson<FileIgnoreDocument>(configurationApiPath(workspaceId, "/fileignore"), jsonRequestInit({ content }, { method: "PUT" }));
}

/**
 * 把 MCP 配置引用、尚未设置的变量写进系统 `.env`（系统级，任何作用域的配置页都写同一处）。
 * 值只写不读；写入后后端重载进程环境，下一次建连即生效。
 */
export function setEnvironmentVariables(values: Record<string, string>): Promise<Record<string, never>> {
  return requestJson("/configuration/environment/variables", jsonRequestInit({ values }, { method: "PUT" }));
}
