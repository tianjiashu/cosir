/**
 * 工具执行共享类型。
 *
 * @module shared/toolExecution
 */

/** 工具策略决策。 */
export interface ToolPolicyDecision {
  /** 决策状态。 */
  status: "allow" | "deny";
  /** 风险等级。 */
  risk_level: string;
  /** 决策原因。 */
  reason: string;
  /** 权限级别。 */
  permission: string;
}
