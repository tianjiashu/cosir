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
  /**
   * 绑定的模型名（litellm 路由名，如 ``deepseek/deepseek-v4-flash``）。
   *
   * ``null`` 表示该 Agent 未配置模型（5 个内置 profile 默认 ``null``，无默认模型
   * 策略）；前端在 ``selectedModelName=null`` 时禁止发送消息（``useModelSendGuard``
   * ``no_model_selected`` 拦截），后端 runner 入口兜底 ``ModelNotConfiguredError``。
   */
  model_name: string | null;
  max_steps: number;
  prompt_ref: Record<string, unknown> | null;
}

export interface ListAgentsResponse {
  agents: AgentProfileResponse[];
  default_agent_id: string;
}
