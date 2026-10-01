import { asRecord, type ToolArtifact } from "./types";

export type AgentConfigurationDraftDisplay = {
  kind: "agent-configuration-draft";
  status: "draft";
  agent_id: string;
  role: string;
  description: string;
  system_prompt: string;
};

/** 读取后端生成的配置草稿展示数据；字段不完整时返回 null 进入安全 fallback。 */
export function readAgentConfigurationDraftDisplay(value: unknown): AgentConfigurationDraftDisplay | null {
  const data = asRecord(value);
  if (
    data.kind !== "agent-configuration-draft"
    || data.status !== "draft"
    || typeof data.agent_id !== "string"
    || typeof data.role !== "string"
    || typeof data.description !== "string"
    || typeof data.system_prompt !== "string"
  ) return null;
  return {
    kind: "agent-configuration-draft",
    status: "draft",
    agent_id: data.agent_id,
    role: data.role,
    description: data.description,
    system_prompt: data.system_prompt,
  };
}

export function readAgentConfigurationDraftArtifact(artifact: ToolArtifact) {
  return readAgentConfigurationDraftDisplay(artifact.display_data);
}
