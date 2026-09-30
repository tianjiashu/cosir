import { describe, expect, it } from "vitest";

import type { TransportState } from "@/lib/assistant/contract";
import {
  currentRunInputTokens,
  currentTransportRun,
  findTransportRun,
  readRuntimeTransportState,
  transportContextWindowTotal,
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

describe("runtime transport state 读取契约", () => {
  // 目的：不可信的 thread external state（未提交 / 无 state 的作用域）必须收敛为「未就绪」
  // 语义，而不是在组件里被断言成契约类型后在渲染期抛错。潜在缺陷：整屏渲染失败。
  it("把未就绪的 external state 收敛为 null 而非抛错", () => {
    const snapshot = state();
    expect(readRuntimeTransportState(null)).toBeNull();
    expect(readRuntimeTransportState(undefined)).toBeNull();
    expect(readRuntimeTransportState({})).toBeNull();
    expect(readRuntimeTransportState(snapshot)).toBe(snapshot);
  });

  it("按 runId 定位 Run，并在未就绪或未指定 runId 时返回 undefined", () => {
    expect(findTransportRun(null, 1)).toBeUndefined();
    expect(findTransportRun(state(), null)).toBeUndefined();
    expect(findTransportRun(state(), 999)).toBeUndefined();
    expect(findTransportRun(state(), 2)?.status).toBe("completed");
  });

  it("读取上下文窗口总量与当前 Run 输入 token，未就绪时返回 null", () => {
    expect(transportContextWindowTotal(null)).toBeNull();
    expect(transportContextWindowTotal({ ...state(), context_window_total: 900 })).toBe(900);
    expect(currentRunInputTokens(null)).toBeNull();
    expect(currentRunInputTokens(state())).toBeNull();
  });
});
