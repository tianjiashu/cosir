import { describe, expect, it } from "vitest";

import type { TransportState } from "@/lib/assistant/contract";
import {
  currentTransportRun,
  findTransportRun,
  readRuntimeTransportState,
  latestMeasuredContextUsage,
  transportMessageCount,
} from "@/lib/assistant/transport-state-operations";

function state(): TransportState {
  return {
    runs: [
      {
        runId: 1,
        status: "running",
        endReason: null,
        contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] },
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
        contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] },
        messages: [{ id: "assistant-2", role: "assistant", parts: [] }],
        usage: null,
        error: null,
      },
    ],
    current_run_id: 1,
    approvals: {},
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

  it("未就绪或尚无有效测量时返回 null", () => {
    expect(latestMeasuredContextUsage(null)).toBeNull();
    expect(latestMeasuredContextUsage(state())).toBeNull();
  });
});

const measuredRun = (runId: number, used: number | null, total: number | null): TransportState["runs"][number] => ({
  runId, status: "completed", endReason: "stop", contextWindowTotal: total, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] },
  messages: [], error: null, usage: { input_tokens: used, output_tokens: null, total_tokens: null,
    cache_hit_tokens: null, cache_miss_tokens: null, reasoning_tokens: null },
});

describe("最新有效上下文测量", () => {
  it("按 Run id 选择最近一次测量，窗口和用量始终来自同一 Run", () => {
    const snapshot = { ...state(), runs: [measuredRun(8, 400, 800), measuredRun(3, 50, 100)], current_run_id: 3 };
    expect(latestMeasuredContextUsage(snapshot)).toEqual({ used: 400, total: 800 });
  });

  it.each([null, 0])("新 Run 尚无用量（%s）时保留之前的测量，不混用新模型窗口", (used) => {
    const snapshot = { ...state(), runs: [measuredRun(1, 50, 100), measuredRun(2, used, 800)], current_run_id: 2 };
    expect(latestMeasuredContextUsage(snapshot)).toEqual({ used: 50, total: 100 });
    snapshot.runs[1].usage!.input_tokens = 200;
    expect(latestMeasuredContextUsage(snapshot)).toEqual({ used: 200, total: 800 });
  });

  it.each([null, 0])("新 Run 没有有效窗口（%s）时不覆盖已有测量", (total) => {
    expect(latestMeasuredContextUsage({ ...state(), runs: [measuredRun(1, 50, 100), measuredRun(2, 200, total)] }))
      .toEqual({ used: 50, total: 100 });
  });
});
