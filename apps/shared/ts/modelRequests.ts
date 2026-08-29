/**
 * 后端 API 共享类型定义。
 *
 * 本文件由 `scripts/generate_api_ts.py` 从后端 Pydantic API schema 生成。
 * 不要手动修改；请先更新 `apps/backend/app/api/schemas/` 后重新生成。
 *
 * @module shared/modelRequests
 */
export interface ProviderCreateRequest {
  name: string;
  model_name: string;
  base_url?: string | null;
  api_key?: string | null;
  sort_order?: number;
}

export interface ProviderUpdateRequest {
  base_url?: string | null;
  api_key?: string | null;
  enabled?: boolean | null;
  sort_order?: number | null;
}
