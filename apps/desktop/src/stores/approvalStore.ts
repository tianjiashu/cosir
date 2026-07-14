/**
 * 工具审批前端状态 Store。
 *
 * @module stores/approvalStore
 */

import { create } from "zustand";
import type { ApprovalRequestRecord } from "@shared/approvals";

/** 审批 Store 状态。 */
interface ApprovalState {
  /** 待处理审批请求。 */
  pendingApprovals: ApprovalRequestRecord[];
  /** 当前正在提交的审批 ID。 */
  submittingApprovalId: string | null;
  /** 最近一次审批错误。 */
  approvalError: string | null;
}

/** 审批 Store 动作。 */
interface ApprovalActions {
  /** 覆盖待处理审批请求。 */
  setPendingApprovals: (approvals: ApprovalRequestRecord[]) => void;
  /** 设置正在提交的审批 ID。 */
  setSubmittingApprovalId: (approvalId: string | null) => void;
  /** 设置审批错误。 */
  setApprovalError: (message: string | null) => void;
  /** 从待处理列表移除审批请求。 */
  removeApproval: (approvalId: string) => void;
}

/**
 * 工具审批 Zustand Store。
 *
 * 只保存审批领域状态，不发起 HTTP 请求，不渲染 JSX。
 */
export const useApprovalStore = create<ApprovalState & ApprovalActions>((set) => ({
  pendingApprovals: [],
  submittingApprovalId: null,
  approvalError: null,

  setPendingApprovals: (approvals) => set({ pendingApprovals: approvals }),
  setSubmittingApprovalId: (approvalId) => set({ submittingApprovalId: approvalId }),
  setApprovalError: (message) => set({ approvalError: message }),
  removeApproval: (approvalId) =>
    set((state) => ({
      pendingApprovals: state.pendingApprovals.filter((approval) => approval.approval_id !== approvalId),
    })),
}));
