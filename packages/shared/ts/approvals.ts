/**
 * 工具审批共享类型。
 *
 * @module shared/approvals
 */

/** 待处理审批请求。 */
export interface ApprovalRequestRecord {
  /** 审批请求标识符。 */
  approval_id: string;
  /** 所属运行标识符。 */
  run_id: string;
  /** 关联步骤标识符。 */
  step_id: string | null;
  /** 关联工具调用标识符。 */
  tool_call_id: string | null;
  /** 工具名称。 */
  tool_name: string;
  /** 权限级别。 */
  permission: string;
  /** 风险等级。 */
  risk_level: string;
  /** 面向 UI 的审批详情。 */
  payload: Record<string, unknown>;
  /** 审批状态。 */
  status: string;
  /** 创建时间。 */
  created_at: string;
  /** 决策时间。 */
  decided_at: string | null;
}

/** 审批决策响应。 */
export interface ApprovalDecisionRecord {
  /** 决策标识符。 */
  decision_id: string;
  /** 审批请求标识符。 */
  approval_id: string;
  /** 决策值。 */
  decision: "approved" | "denied";
  /** 决策原因。 */
  reason: string | null;
  /** 决策时间。 */
  decided_at: string;
  /** 幂等键。 */
  idempotency_key: string;
}
