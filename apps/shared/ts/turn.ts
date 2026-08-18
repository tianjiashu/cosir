/**
 * 后端 API 共享类型定义。
 *
 * 本文件由 `scripts/generate_api_ts.py` 从后端 Pydantic API schema 生成。
 * 不要手动修改；请先更新 `apps/backend/app/api/schemas/` 后重新生成。
 *
 * @module shared/turn
 */
export type TurnStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "reverted";

export interface TurnRecord {
  turn_id: string;
  task_id: string;
  input_text: string;
  status: TurnStatus;
  end_reason?: string | null;
  response_text?: string | null;
  agent_id?: string | null;
  model_name?: string | null;
  created_at: string;
  updated_at: string;
}
