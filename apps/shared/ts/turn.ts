/**
 * 轮次共享类型定义。
 *
 * 与后端 `TurnRecord.to_dict()` 输出保持一致，
 * 作为前后端共享的 turn 契约事实源。
 *
 * @module shared/turn
 */

/** 轮次状态枚举，与后端 TurnRecord.status 可能值对齐。 */
export type TurnStatus = "pending" | "running" | "completed" | "failed" | "cancelled";

/** 轮次记录接口，对应后端 `TurnRecord.to_dict()` 输出。 */
export interface TurnRecord {
  /** 唯一的轮次标识符。 */
  turn_id: string;
  /** 关联的任务标识符。 */
  task_id: string;
  /** 本轮用户输入文本。 */
  input_text: string;
  /** 当前轮次状态。 */
  status: TurnStatus;
  /** 轮次创建时间戳。 */
  created_at: string;
  /** 轮次最近更新时间戳。 */
  updated_at: string;
}
