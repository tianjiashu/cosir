/**
 * 后端 API 共享类型定义。
 *
 * 本文件由 `scripts/generate_api_ts.py` 从后端 Pydantic API schema 生成。
 * 不要手动修改；请先更新 `apps/backend/app/api/schemas/` 后重新生成。
 *
 * @module shared/agents
 */

export interface AgentProfileResponse {
  agent_id: string;
  role: string;
  description: string;
  allowed_tools: string[];
  workflow: string;
  model_name: string;
  max_steps: number;
  prompt_ref: Record<string, unknown> | null;
}

export interface ListAgentsResponse {
  agents: AgentProfileResponse[];
  default_agent_id: string;
}
