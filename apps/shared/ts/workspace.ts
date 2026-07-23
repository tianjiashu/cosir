/**
 * 后端 API 共享类型定义。
 *
 * 本文件由 `scripts/generate_api_ts.py` 从后端 Pydantic API schema 生成。
 * 不要手动修改；请先更新 `apps/backend/app/api/schemas/` 后重新生成。
 *
 * @module shared/workspace
 */

export interface WorkspaceResponse {
  workspace_id: string;
  name: string;
  root_path: string;
  created_at: string;
  updated_at: string;
}

export type WorkspaceRecord = WorkspaceResponse;
