/**
 * 任务与会话状态类型定义。
 *
 * 与后端 `app/storage/records.py` 保持字段一致，
 * 作为前后端共享的任务状态契约事实源。
 *
 * @module shared/task
 */

/** 任务状态枚举，与后端 TaskRecord.status 可能值对齐。注意：后端完成态为 "completed" 而非 "finished"。 */
export type TaskStatus = "pending" | "running" | "completed" | "failed" | "cancelled";

/** 任务记录接口，对应后端 `TaskRecord.to_dict()` 输出。 */
export interface TaskRecord {
  /** 唯一的任务标识符（UUID）。 */
  task_id: string;
  /** 关联的会话标识符。 */
  session_id: string;
  /** 负责执行任务的 Agent 标识符。 */
  agent_id: string;
  /** 原始的纯文本用户任务输入。 */
  input_text: string;
  /** 当前任务状态。 */
  status: TaskStatus;
  /** 任务创建时的 UTC 时间戳（ISO-8601）。 */
  created_at: string;
  /** 任务最近更新时的 UTC 时间戳（ISO-8601）。 */
  updated_at: string;
}

/** 会话记录接口，对应后端 `SessionRecord`。 */
export interface SessionRecord {
  /** 唯一的会话标识符。 */
  session_id: string;
  /** 与会话关联的项目路径。 */
  project_path: string | null;
  /** 会话创建时间戳。 */
  created_at: string;
  /** 会话最近更新时间戳。 */
  updated_at: string;
}

/** 检查点记录接口，对应后端 `CheckpointRecord.to_dict()` 输出。 */
export interface CheckpointRecord {
  /** 唯一的检查点标识符。 */
  checkpoint_id: string;
  /** 关联的任务标识符。 */
  task_id: string;
  /** 产出该检查点的运行时阶段。 */
  stage: string;
  /** 简短的、人类可读的检查点摘要。 */
  summary: string;
  /** 可序列化为 JSON 的运行时状态快照。 */
  snapshot: Record<string, unknown>;
  /** 检查点创建时间戳。 */
  created_at: string;
}
