/**
 * 任务共享类型定义。
 *
 * 与后端 `TaskRecord.to_dict()` 输出保持一致，作为前后端共享的任务契约事实源。
 *
 * @module shared/task
 */

/** 任务状态类型，覆盖后端生命周期 status 与派生 execution_status 的可能值。 */
export type TaskStatus =
  | "pending"
  | "running"
  | "active"
  | "completed"
  | "failed"
  | "cancelled"
  | "empty"
  | "open"
  | "archived";

/** 任务记录接口，对应后端 `TaskRecord.to_dict()` 输出。 */
export interface TaskRecord {
  /** 唯一任务标识。 */
  task_id: string;
  /** 关联的工作区标识。 */
  workspace_id: string;
  /** 负责执行任务的 Agent 标识。 */
  agent_id: string;
  /** 原始用户任务输入。 */
  input_text: string;
  /** 任务标题。 */
  title: string;
  /** 最近一条消息预览。 */
  last_message_preview: string;
  /** 最新轮次标识；无轮次时为 null。 */
  latest_turn_id: string | null;
  /** 当前任务生命周期状态。 */
  status: TaskStatus;
  /** 后端从最新 turn 派生的执行状态；列表接口可能为 null。 */
  execution_status: TaskStatus | null;
  /** 任务创建 UTC 时间戳。 */
  created_at: string;
  /** 任务最近更新 UTC 时间戳。 */
  updated_at: string;
}
