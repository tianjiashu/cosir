/**
 * 后端 API 共享类型定义。
 *
 * 本文件由 `scripts/generate_api_ts.py` 从后端 Pydantic API schema 生成。
 * 不要手动修改；请先更新 `apps/backend/app/api/schemas/` 后重新生成。
 *
 * @module shared/task
 */
export type TaskStatus = string;

export interface TaskRecord {
  task_id: string;
  workspace_id: string;
  agent_id: string;
  title: string;
  status: TaskStatus;
  execution_status?: TaskStatus | null;
  task_type?: string;
  parent_task_id?: string | null;
  parent_turn_id?: string | null;
  delegation_id?: string | null;
  created_at: string;
  updated_at: string;
}
