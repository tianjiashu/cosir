import { describe, expect, it } from "vitest";
import { useRunStore } from "@/stores/runStore";

describe("runStore", () => {
  it("stores recoverable runs without side effects", () => {
    useRunStore.getState().resetRunState();

    useRunStore.getState().setRecoverableRuns([
      {
        run_id: "run-1",
        task_id: "task-1",
        thread_id: "thread-1",
        status: "waiting",
        wait_reason: "approval",
        active_step_id: null,
        active_wait_id: "approval-1",
        last_checkpoint_id: null,
        interruption_reason: null,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ]);

    expect(useRunStore.getState().recoverableRuns).toHaveLength(1);
    expect(useRunStore.getState().recoverableRuns[0].wait_reason).toBe("approval");
  });
});
