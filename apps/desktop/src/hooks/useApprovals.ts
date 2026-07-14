/**
 * 工具审批编排 Hook。
 *
 * @module hooks/useApprovals
 */

import { useCallback } from "react";
import { fetchPendingApprovals, submitApprovalDecision } from "@/services/approvals";
import { useApprovalStore } from "@/stores/approvalStore";
import { logError } from "@/lib/logger";

/**
 * 提供审批查询和 approve / deny 编排。
 *
 * @returns 审批状态和操作函数。
 *
 * @sideeffect 调用 approvals service、写入 approvalStore，并通过统一 logger 记录错误。
 */
export function useApprovals() {
  const pendingApprovals = useApprovalStore((state) => state.pendingApprovals);
  const submittingApprovalId = useApprovalStore((state) => state.submittingApprovalId);
  const approvalError = useApprovalStore((state) => state.approvalError);
  const setPendingApprovals = useApprovalStore((state) => state.setPendingApprovals);
  const setSubmittingApprovalId = useApprovalStore((state) => state.setSubmittingApprovalId);
  const setApprovalError = useApprovalStore((state) => state.setApprovalError);
  const removeApproval = useApprovalStore((state) => state.removeApproval);

  const refreshApprovals = useCallback(
    async (taskId: string) => {
      setApprovalError(null);
      try {
        setPendingApprovals(await fetchPendingApprovals(taskId));
      } catch (err) {
        setApprovalError(err instanceof Error ? err.message : String(err));
        logError("刷新审批请求失败", err, { module: "useApprovals", taskId });
      }
    },
    [setApprovalError, setPendingApprovals],
  );

  const decideApproval = useCallback(
    async (approvalId: string, decision: "approved" | "denied", reason?: string) => {
      setSubmittingApprovalId(approvalId);
      setApprovalError(null);
      try {
        await submitApprovalDecision(approvalId, {
          decision,
          reason,
          idempotency_key: `approval:${approvalId}:${decision}`,
        });
        removeApproval(approvalId);
      } catch (err) {
        setApprovalError(err instanceof Error ? err.message : String(err));
        logError("提交审批决策失败", err, { module: "useApprovals", approvalId, decision });
      } finally {
        setSubmittingApprovalId(null);
      }
    },
    [removeApproval, setApprovalError, setSubmittingApprovalId],
  );

  return {
    pendingApprovals,
    submittingApprovalId,
    approvalError,
    refreshApprovals,
    decideApproval,
  };
}
