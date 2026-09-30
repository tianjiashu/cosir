import { describe, expect, it } from "vitest";

import {
  formatRunIdentity,
  shouldDisplayRunIdentity,
} from "@/components/assistant/run-identity-display";

describe("run identity display", () => {
  it("formats task and run identifiers for diagnostics", () => {
    expect(formatRunIdentity(12, 45)).toBe("task_id=12\nrun_id=45");
  });

  it.each(["completed", "failed", "cancelled"]) (
    "shows identifiers for the %s terminal state",
    (status) => {
      expect(shouldDisplayRunIdentity({
        taskId: 12,
        runId: 45,
        status,
        visible: true,
      })).toBe(true);
    },
  );

  it.each(["pending", "running", null]) (
    "hides identifiers before the %s terminal state",
    (status) => {
      expect(shouldDisplayRunIdentity({
        taskId: 12,
        runId: 45,
        status,
        visible: true,
      })).toBe(false);
    },
  );

  it("hides identifiers when the message is not the final Run message", () => {
    expect(shouldDisplayRunIdentity({
      taskId: 12,
      runId: 45,
      status: "completed",
      visible: false,
    })).toBe(false);
  });
});
