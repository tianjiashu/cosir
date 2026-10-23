import { describe, expect, it } from "vitest";

import { parseTransportState, TransportSnapshotValidationError } from "@/lib/assistant/snapshot-validation";
import type { TransportState } from "@/lib/assistant/contract";

const validSnapshot = (): TransportState => ({
  runs: [],
  current_run_id: null,
  approvals: {},
  error: null,
});

describe("Transport snapshot validation", () => {
  it("returns the complete server snapshot without creating fields", () => {
    const snapshot = validSnapshot();
    expect(parseTransportState(snapshot)).toBe(snapshot);
  });

  it.each([null, 0, 128_000])("接受 Run 级窗口值 %s，保持原快照不补字段", (contextWindowTotal) => {
    const snapshot = { ...validSnapshot(), runs: [{
      runId: 1, status: "completed", endReason: "stop",
      contextWindowTotal, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] }, messages: [], usage: null, error: null,
    }], current_run_id: 1 };
    expect(parseTransportState(snapshot)).toBe(snapshot);
  });

  it.each([undefined, -1, 0.5, "128000", true, NaN, Infinity])("拒绝非法 Run 窗口值 %s", (contextWindowTotal) => {
    const snapshot = { ...validSnapshot(), runs: [{
      runId: 1, status: "completed", endReason: "stop",
      contextWindowTotal, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] }, messages: [], usage: null, error: null,
    }], current_run_id: 1 };
    expect(() => parseTransportState(snapshot)).toThrow("runs[0].contextWindowTotal");
  });

  it("拒绝旧 Task 级窗口字段，以及缺失 Run 窗口字段的快照", () => {
    expect(() => parseTransportState({ ...validSnapshot(), context_window_total: 128_000 }))
      .toThrow(TransportSnapshotValidationError);
    const snapshot = { ...validSnapshot(), runs: [{
      runId: 1, status: "completed", endReason: "stop",
      messages: [], usage: null, error: null,
    }], current_run_id: 1 };
    expect(() => parseTransportState(snapshot)).toThrow("runs[0]");
  });

  it("rejects a missing runs field instead of treating it as an empty conversation", () => {
    const snapshot = validSnapshot();
    delete (snapshot as { runs?: unknown }).runs;
    expect(() => parseTransportState(snapshot)).toThrow(TransportSnapshotValidationError);
    expect(() => parseTransportState(snapshot)).toThrow("runs");
  });

  it("rejects invalid tool lifecycle state with a diagnostic path", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [{ runId: 1, status: "running", endReason: null, contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] }, usage: null, error: null, messages: [{
      id: "m1",
      role: "assistant",
      parts: [{ type: "tool-call", toolCallId: "t1", toolName: "read_file", status: "future", args: {} }],
    }] }];
    snapshot.current_run_id = 1;
    expect(() => parseTransportState(snapshot)).toThrow("runs[0].messages[0].parts[0].status");
  });

  it("accepts a positive child Run locator and rejects invalid locators", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [{ runId: 1, status: "running", endReason: null, contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] }, usage: null, error: null, messages: [{
      id: "m1",
      role: "assistant",
      parts: [{
        type: "tool-call",
        toolCallId: "delegate-1",
        toolName: "delegate_task",
        status: "running",
        args: {},
        child_task_id: 501,
      }],
    }] }];
    snapshot.current_run_id = 1;

    expect(parseTransportState(snapshot)).toBe(snapshot);
    const invalidSnapshot = structuredClone(snapshot);
    const toolPart = invalidSnapshot.runs[0].messages[0].parts[0] as { child_task_id: number };
    toolPart.child_task_id = 0;
    expect(() => parseTransportState(invalidSnapshot)).toThrow("child_task_id");
  });

  it("accepts the minimal child session display contract", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [{ runId: 1, status: "completed", endReason: "stop", contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] }, usage: null, error: null, messages: [{
      id: "m1",
      role: "assistant",
      parts: [{
        type: "tool-call",
        toolCallId: "delegate-1",
        toolName: "delegate_task",
        status: "completed",
        args: {},
        display_data: {
          kind: "delegation-result",
          title: "审查代码",
          role: "Reviewer",
          child_task_id: 501,
          status: "completed",
        },
      }],
    }] }];
    snapshot.current_run_id = 1;

    expect(parseTransportState(snapshot)).toBe(snapshot);
  });

  it("accepts the child agent send/status result contract", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [{ runId: 1, status: "completed", endReason: "stop", contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] }, usage: null, error: null, messages: [{
      id: "m1",
      role: "assistant",
      parts: [{
        type: "tool-call",
        toolCallId: "status-1",
        toolName: "child_agent_status",
        status: "completed",
        args: {},
        display_data: {
          kind: "child-agent-result",
          operation: "status",
          child_task_id: 501,
          status: "completed",
          agent_id: "reviewer",
          agent_name: "审查代码",
          final_output: "已完成审查。",
          end_reason: null,
        },
      }],
    }] }];
    snapshot.current_run_id = 1;
    expect(parseTransportState(snapshot)).toBe(snapshot);
  });

  it("rejects child output embedded in a parent delegation display", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [{ runId: 1, status: "completed", endReason: "stop", contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] }, usage: null, error: null, messages: [{
      id: "m1",
      role: "assistant",
      parts: [{
        type: "tool-call",
        toolCallId: "delegate-1",
        toolName: "delegate_task",
        status: "completed",
        args: {},
        display_data: {
          kind: "delegation-result",
          title: "审查代码",
          child_task_id: 501,
          status: "completed",
          final_output: "不应进入父级委派卡片",
        },
      }],
    }] }];
    snapshot.current_run_id = 1;

    expect(() => parseTransportState(snapshot)).toThrow("已知字段");
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
    snapshot.runs = [{ runId: 1, status: "running", endReason: null, contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] }, usage: null, error: null, messages: [] }];
    expect(() => parseTransportState(snapshot)).toThrow("current_run_id");
  });

  it("rejects multiple active runs instead of silently choosing one to attach", () => {
    const snapshot = validSnapshot();
    snapshot.runs = [
      { runId: 1, status: "running", endReason: null, contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] }, usage: null, error: null, messages: [] },
      { runId: 2, status: "pending", endReason: null, contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] }, usage: null, error: null, messages: [] },
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
      contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] },
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
      contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] },
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
      contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] },
      usage: null,
      error: null,
      messages: [],
    }];
    snapshot.current_run_id = 1;
    delete (snapshot.runs[0] as { error?: unknown }).error;
    expect(() => parseTransportState(snapshot)).toThrow("runs[0]");
  });
});
