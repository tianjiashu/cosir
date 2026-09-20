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
    snapshot.runs = [{ runId: 1, status: "running", endReason: null, usage: null, error: null, messages: [{
      id: "m1",
      role: "assistant",
      parts: [{ type: "tool-call", toolCallId: "t1", toolName: "read_file", status: "future", args: {} }],
    }] }];
    snapshot.current_run_id = 1;
    expect(() => parseTransportState(snapshot)).toThrow("runs[0].messages[0].parts[0].status");
  });

  it("accepts a positive child Run locator and rejects invalid locators", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [{ runId: 1, status: "running", endReason: null, usage: null, error: null, messages: [{
      id: "m1",
      role: "assistant",
      parts: [{
        type: "tool-call",
        toolCallId: "delegate-1",
        toolName: "delegate_task",
        status: "running",
        args: {},
        child_task_id: 501,
        child_run_id: 902,
      }],
    }] }];
    snapshot.current_run_id = 1;

    expect(parseTransportState(snapshot)).toBe(snapshot);
    const invalidSnapshot = structuredClone(snapshot);
    const toolPart = invalidSnapshot.runs[0].messages[0].parts[0] as { child_run_id: number };
    toolPart.child_run_id = 0;
    expect(() => parseTransportState(invalidSnapshot)).toThrow("child_run_id");
  });

  it("accepts a code+message snapshot error and rejects the legacy retryable field", () => {
    const snapshot = validSnapshot();
    snapshot.error = { code: "run_failed", message: "运行失败" };
    expect(parseTransportState(snapshot)).toBe(snapshot);

    // retryable 只属于工具观察（面向模型），不属于 Transport 错误契约。
    const legacy = structuredClone(snapshot) as unknown as { error: Record<string, unknown> };
    legacy.error = { code: "run_failed", message: "运行失败", retryable: false };
    expect(() => parseTransportState(legacy)).toThrow(TransportSnapshotValidationError);
    expect(() => parseTransportState(legacy)).toThrow("error");
  });

  it("rejects an active run that is not the current run", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [{ runId: 1, status: "running", endReason: null, usage: null, error: null, messages: [] }];
    expect(() => parseTransportState(snapshot)).toThrow("current_run_id");
  });

  it("rejects multiple active runs instead of silently choosing one to attach", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [
      { runId: 1, status: "running", endReason: null, usage: null, error: null, messages: [] },
      { runId: 2, status: "pending", endReason: null, usage: null, error: null, messages: [] },
    ];
    snapshot.current_run_id = 1;
    expect(() => parseTransportState(snapshot)).toThrow("最多一个 active Run");
  });

  it("accepts a controlled run error payload", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [{
      runId: 1,
      status: "failed",
      endReason: "model_insufficient_quota",
      usage: null,
      error: { code: "model_insufficient_quota", message: "模型服务配额或余额不足，请充值或更换模型" },
      messages: [],
    }];
    snapshot.current_run_id = 1;
    expect(parseTransportState(snapshot)).toBe(snapshot);
  });

  it("rejects a run error that is missing its message", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [{
      runId: 1,
      status: "failed",
      endReason: "model_insufficient_quota",
      usage: null,
      error: null,
      messages: [],
    }];
    snapshot.current_run_id = 1;
    (snapshot.runs[0] as { error: unknown }).error = { code: "model_insufficient_quota" };
    expect(() => parseTransportState(snapshot)).toThrow("runs[0].error");
  });

  it("rejects a run without the error field instead of tolerating a partial snapshot", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [{
      runId: 1,
      status: "running",
      endReason: null,
      usage: null,
      error: null,
      messages: [],
    }];
    snapshot.current_run_id = 1;
    delete (snapshot.runs[0] as { error?: unknown }).error;
    expect(() => parseTransportState(snapshot)).toThrow("runs[0]");
  });
});
