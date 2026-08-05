/**
 * 后端 API 共享类型定义。
 *
 * 本文件由 `scripts/generate_api_ts.py` 从后端 Pydantic API schema 生成。
 * 不要手动修改；请先更新 `apps/backend/app/api/schemas/` 后重新生成。
 *
 * @module shared/changes
 */

export interface ChangeFile {
  path: string;
  action: string;
  status: 'pending' | 'kept' | 'reverted';
  last_tool_call_id: string;
  last_turn_id: string;
  additions: number;
  deletions: number;
}

export interface ChangeCheckpoint {
  turn_id: string;
  turn_seq: number;
  label: string;
}

export interface ChangeSet {
  task_id: string;
  checkpoints: ChangeCheckpoint[];
  files: ChangeFile[];
}
