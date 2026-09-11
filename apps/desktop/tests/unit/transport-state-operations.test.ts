import { describe, expect, it } from "vitest";

import type { TransportState } from "@/lib/assistant/contract";
import {
  currentTransportRun,
  markTransportStateCancelled,
  transportMessageCount,
} from "@/lib/assistant/transport-state-operations";

function state(): TransportState {
  return {
    runs: [
      {
        runId: 1,
        status: "running",
        endReason: null,
        messages: [{
          id: "assistant-1",
          role: "assistant",
          parts: [
            { type: "tool-call", toolCallId: "tool-1", toolName: "search_files", status: "pending", args: {} },
            { type: "tool-call", toolCallId: "tool-2", toolName: "read_file", status: "running", args: {} },
          ],
        }],
        usage: null,
      },
      {
        runId: 2,
        status: "completed",
        endReason: null,
        messages: [{ id: "assistant-2", role: "assistant", parts: [] }],
        usage: null,
      },
    ],
    current_run_id: 1,
    approvals: {},
    context_usage_ratio: null,
    context_usage_used: null,
    context_window_total: null,
    error: null,
  };
}

describe("transport state operations", () => {
  it("finds the canonical current run and counts messages across runs", () => {
    const snapshot = state();
    expect(currentTransportRun(snapshot)?.runId).toBe(1);
    expect(transportMessageCount(snapshot)).toBe(2);
  });

  it("projects cancellation to active tool parts without mutating the input", () => {
    const snapshot = state();
    const cancelled = markTransportStateCancelled(snapshot, 1);
    const parts = cancelled.runs[0]?.messages[0]?.parts;

    expect(snapshot.runs[0]?.status).toBe("running");
    expect(cancelled.current_run_id).toBe(1);
    expect(cancelled.runs[0]?.status).toBe("cancelled");
    expect(parts).toEqual([
      expect.objectContaining({ status: "cancelled", error: "已取消", isError: false }),
      expect.objectContaining({ status: "cancelled", error: "已取消", isError: false }),
    ]);
  });
});
