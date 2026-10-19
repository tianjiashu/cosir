import { describe, expect, it } from "vitest";

import {
  formatRunDiagnostics,
  shouldOfferRunDiagnostics,
} from "@/components/assistant/run-diagnostics-actions";

describe("run diagnostics actions", () => {
  it("formats task and run identifiers as grep-friendly lines", () => {
    expect(formatRunDiagnostics({ taskId: 12, runId: 45, status: "completed", run: null }))
      .toBe("task_id=12\nrun_id=45\nrun_status=completed");
  });

  it("appends end reason and error code when the run provides them", () => {
    expect(formatRunDiagnostics({
      taskId: 12,
      runId: 45,
      status: "failed",
      run: {
        endReason: "model_error",
        error: { code: "MODEL_HTTP_ERROR", message: "上游返回 500" },
      },
    })).toBe([
      "task_id=12",
      "run_id=45",
      "run_status=failed",
      "end_reason=model_error",
      "error_code=MODEL_HTTP_ERROR",
    ].join("\n"));
  });

  it.each(["completed", "failed", "cancelled"])(
    "offers diagnostics for the %s terminal state",
    (status) => {
      expect(shouldOfferRunDiagnostics({
        taskId: 12,
        runId: 45,
        status,
        visible: true,
      })).toBe(true);
    },
  );

  it.each(["pending", "running", null])(
    "hides diagnostics before the %s terminal state",
    (status) => {
      expect(shouldOfferRunDiagnostics({
        taskId: 12,
        runId: 45,
        status,
        visible: true,
      })).toBe(false);
    },
  );

  it("hides diagnostics when the message is not the final Run message", () => {
    expect(shouldOfferRunDiagnostics({
      taskId: 12,
      runId: 45,
      status: "completed",
      visible: false,
    })).toBe(false);
  });
});
