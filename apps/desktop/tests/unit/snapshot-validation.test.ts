import { describe, expect, it } from "vitest";

import { parseTransportState, TransportSnapshotValidationError } from "@/lib/assistant/snapshot-validation";
import type { TransportState } from "@/lib/assistant/contract";

const validSnapshot = (): TransportState => ({
  messages: [],
  run: { runId: null, status: "idle" },
  approvals: {},
  context_usage: 0,
  usage: {
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cache_hit_tokens: 0,
    cache_miss_tokens: 0,
    reasoning_tokens: 0,
  },
  error: null,
});

describe("Transport snapshot validation", () => {
  it("returns the complete server snapshot without creating fields", () => {
    const snapshot = validSnapshot();
    expect(parseTransportState(snapshot)).toBe(snapshot);
  });

  it("rejects a missing messages field instead of treating it as an empty conversation", () => {
    const snapshot = validSnapshot();
    delete (snapshot as { messages?: unknown }).messages;
    expect(() => parseTransportState(snapshot)).toThrow(TransportSnapshotValidationError);
    expect(() => parseTransportState(snapshot)).toThrow("messages");
  });

  it("rejects invalid tool lifecycle state with a diagnostic path", () => {
    const snapshot = validSnapshot();
    snapshot.messages = [{
      id: "m1",
      role: "assistant",
      status: "running",
      endReason: null,
      parts: [{ type: "tool-call", toolCallId: "t1", toolName: "read_file", status: "future", args: {} }],
    }];
    expect(() => parseTransportState(snapshot)).toThrow("messages[0].parts[0].status");
  });
});
