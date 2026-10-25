import { describe, expect, it } from "vitest";

import {
  isActivityActive,
  readRuntimeTransportState,
  latestMeasuredContextUsage,
  transportMessageCount,
} from "@/lib/assistant/transport-state-operations";
import { activity, assistantMessage, outcome, queuedMessage, transportState, usage, userMessage } from "./transport-fixtures";

describe("transport state operations", () => {
  it("counts canonical messages without queued inputs", () => {
    const snapshot = transportState({
      messages: [userMessage("1", 1), assistantMessage("assistant-1", 1)],
      queued_messages: [queuedMessage("2")],
    });
    expect(transportMessageCount(snapshot)).toBe(2);
  });

  it.each([
    [null, false],
    ["running", true],
    ["suspended", true],
    ["completed", false],
    ["failed", false],
    ["cancelled", false],
  ] as const)("活动 %s → 活动态 %s", (status, expected) => {
    const snapshot = transportState({ activity: status === null ? null : activity(1, status) });
    expect(isActivityActive(snapshot)).toBe(expected);
  });
});

describe("runtime transport state 读取契约", () => {
  // 目的：不可信的 thread external state（未提交 / 无 state 的作用域）必须收敛为「未就绪」
  // 语义，而不是在组件里被断言成契约类型后在渲染期抛错。潜在缺陷：整屏渲染失败。
  it("把未就绪的 external state 收敛为 null 而非抛错", () => {
    const snapshot = transportState({ activity: activity(1, "running") });
    expect(readRuntimeTransportState(null)).toBeNull();
    expect(readRuntimeTransportState(undefined)).toBeNull();
    expect(readRuntimeTransportState({})).toBeNull();
    expect(readRuntimeTransportState({ runs: [], current_run_id: null })).toBeNull();
    expect(readRuntimeTransportState(snapshot)).toBe(snapshot);
  });

  it("未就绪或尚无有效测量时返回 null", () => {
    expect(latestMeasuredContextUsage(null)).toBeNull();
    expect(latestMeasuredContextUsage(transportState())).toBeNull();
    expect(latestMeasuredContextUsage(transportState({ activity: activity(1, "running", { contextWindowTotal: 800 }) }))).toBeNull();
  });
});

describe("最新有效上下文测量", () => {
  it("当前活动有用量时取活动的输入 token 与窗口", () => {
    const snapshot = transportState({
      activity: activity(3, "running", { contextWindowTotal: 800, usage: usage({ input_tokens: 400 }) }),
    });
    expect(latestMeasuredContextUsage(snapshot)).toEqual({ used: 400, total: 800 });
  });

  it.each([null, 0])("新一轮尚无用量（%s）时回落到最近一条带用量的回复结局，窗口也取那一轮的", (used) => {
    // 两轮之间换了模型：新一轮窗口 800，上一轮 200。回落时「已用 / 上限」都来自上一轮。
    const snapshot = transportState({
      messages: [
        assistantMessage("assistant-1", 1, [], outcome("completed", { usage: usage({ input_tokens: 50 }), contextWindowTotal: 200 })),
        userMessage("2", 2),
      ],
      activity: activity(2, "running", { contextWindowTotal: 800, usage: usage({ input_tokens: used }) }),
    });
    expect(latestMeasuredContextUsage(snapshot)).toEqual({ used: 50, total: 200 });
  });

  it("回落到的那一轮没有记下窗口时不显示，不拿当前活动的窗口凑", () => {
    const snapshot = transportState({
      messages: [assistantMessage("assistant-1", 1, [], outcome("completed", { usage: usage({ input_tokens: 50 }) })), userMessage("2", 2)],
      activity: activity(2, "running", { contextWindowTotal: 800 }),
    });
    expect(latestMeasuredContextUsage(snapshot)).toBeNull();
  });

  it.each([null, 0])("当前活动没有有效窗口（%s）时不给出测量", (total) => {
    const snapshot = transportState({ activity: activity(2, "running", { contextWindowTotal: total, usage: usage({ input_tokens: 200 }) }) });
    expect(latestMeasuredContextUsage(snapshot)).toBeNull();
  });
});
