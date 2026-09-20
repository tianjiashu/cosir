import { describe, expect, it } from "vitest";

import type { TransportState } from "@/lib/assistant/contract";
import {
  currentTransportRun,
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
            { type: "tool-call", toolCallId: "tool-1", toolName: "search_content", status: "pending", args: {} },
            { type: "tool-call", toolCallId: "tool-2", toolName: "read_file", status: "running", args: {} },
          ],
        }],
        usage: null,
        error: null,
      },
      {
        runId: 2,
        status: "completed",
        endReason: null,
        messages: [{ id: "assistant-2", role: "assistant", parts: [] }],
        usage: null,
        error: null,
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

});
