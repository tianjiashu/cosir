import { describe, expect, it } from "vitest";

import { parseTransportState, TransportSnapshotValidationError } from "@/lib/assistant/snapshot-validation";
import type { TransportState } from "@/lib/assistant/contract";

const validSnapshot = (): TransportState => ({
  runs: [],
  current_run_id: null,
  approvals: {},
  context_usage_ratio: null,
  context_usage_used: null,
  context_window_total: null,
  error: null,
});

describe("Transport snapshot validation", () => {
  it("returns the complete server snapshot without creating fields", () => {
    const snapshot = validSnapshot();
    expect(parseTransportState(snapshot)).toBe(snapshot);
  });

  it("rejects a missing runs field instead of treating it as an empty conversation", () => {
    const snapshot = validSnapshot();
    delete (snapshot as { runs?: unknown }).runs;
    expect(() => parseTransportState(snapshot)).toThrow(TransportSnapshotValidationError);
    expect(() => parseTransportState(snapshot)).toThrow("runs");
  });

  it("rejects invalid tool lifecycle state with a diagnostic path", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [{ runId: 1, status: "running", endReason: null, usage: null, messages: [{
      id: "m1",
      role: "assistant",
      parts: [{ type: "tool-call", toolCallId: "t1", toolName: "read_file", status: "future", args: {} }],
    }] }];
    snapshot.current_run_id = 1;
    expect(() => parseTransportState(snapshot)).toThrow("runs[0].messages[0].parts[0].status");
  });

  it("rejects an active run that is not the current run", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [{ runId: 1, status: "running", endReason: null, usage: null, messages: [] }];
    expect(() => parseTransportState(snapshot)).toThrow("current_run_id");
  });

  it("rejects multiple active runs instead of silently choosing one to attach", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [
      { runId: 1, status: "running", endReason: null, usage: null, messages: [] },
      { runId: 2, status: "pending", endReason: null, usage: null, messages: [] },
    ];
    snapshot.current_run_id = 1;
    expect(() => parseTransportState(snapshot)).toThrow("最多一个 active Run");
  });
});
