import { describe, expect, it } from "vitest";
import { useApprovalStore } from "@/stores/approvalStore";

describe("approvalStore", () => {
  it("removes approved request from pending approvals", () => {
    useApprovalStore.getState().setPendingApprovals([
      {
        approval_id: "approval-1",
        run_id: "run-1",
        step_id: null,
        tool_call_id: null,
        tool_name: "write_file",
        permission: "write_file",
        risk_level: "high",
        payload: { path: "note.txt" },
        status: "pending",
        created_at: "2026-01-01T00:00:00Z",
        decided_at: null,
      },
    ]);

    useApprovalStore.getState().removeApproval("approval-1");

    expect(useApprovalStore.getState().pendingApprovals).toHaveLength(0);
  });
});
