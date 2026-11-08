import { describe, expect, it } from "vitest";

import { formatTurnDiagnostics } from "@/components/conversation-runtime/turn-diagnostics-actions";

describe("turn diagnostics", () => {
  it("按回合输出可检索的诊断行，不含 Run 标识", () => {
    expect(formatTurnDiagnostics({ taskId: 12, turn: 3, status: "completed" }))
      .toBe("task_id=12\nturn=3\nrun_status=completed");
  });

  it("结局带结束原因与错误码时追加", () => {
    expect(formatTurnDiagnostics({
      taskId: 12,
      turn: 3,
      status: "failed",
      endReason: "model_error",
      errorCode: "MODEL_HTTP_ERROR",
    })).toBe([
      "task_id=12",
      "turn=3",
      "run_status=failed",
      "end_reason=model_error",
      "error_code=MODEL_HTTP_ERROR",
    ].join("\n"));
  });
});
