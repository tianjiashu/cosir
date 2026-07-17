/**
 * Durable Run State 共享类型。
 *
 * @module shared/runs
 */

/** 可恢复运行状态记录。 */
export interface RunRecord {
  /** 运行记录标识符。 */
  run_id: string;
  /** 关联任务标识符。 */
  task_id: string;
  /** LangGraph checkpointer thread_id。 */
  thread_id: string;
  /** 当前运行状态。 */
  status: string;
  /** 等待原因，例如 approval 或 human_input。 */
  wait_reason: string | null;
  /** 当前活跃步骤标识符。 */
  active_step_id: string | null;
  /** 当前等待点标识符。 */
  active_wait_id: string | null;
  /** 最近稳定 checkpoint 标识符。 */
  last_checkpoint_id: string | null;
  /** 中断或复核原因。 */
  interruption_reason: string | null;
  /** 创建时间。 */
  created_at: string;
  /** 更新时间。 */
  updated_at: string;
}
