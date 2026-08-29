/**
 * 后端 API 共享类型定义。
 *
 * 本文件由 `scripts/generate_api_ts.py` 从后端 Pydantic API schema 生成。
 * 不要手动修改；请先更新 `apps/backend/app/api/schemas/` 后重新生成。
 *
 * @module shared/modelResponses
 */
export interface ProviderResponse {
  provider_id: number;
  name: string;
  type: string;
  base_url?: string | null;
  api_key_configured?: boolean;
  enabled?: boolean;
  model_count?: number;
  sort_order?: number;
  created_at: string;
  updated_at: string;
}

export interface ModelEntryResponse {
  provider_id: number;
  provider_name: string;
  model_name: string;
  supports_thinking: boolean;
  supports_image: boolean;
  supports_video: boolean;
  enabled: boolean;
  api_key_configured: boolean;
  reasoning_effort: Record<string, unknown>;
  sort_order?: number;
}
