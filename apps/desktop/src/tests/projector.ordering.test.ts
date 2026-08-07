/**
 * timeline 投影器排序 / 合并缺陷验证测试。
 *
 * 覆盖疑似缺陷：
 * 1. `projectTool` 更新既有条目时无条件覆写 `resultSummary` / `result` 等字段，
 *    若 finished 先于 started 到达（或同 callId 复用），会丢失 started 的 arguments。
 * 2. `toolByCallId` 存的是 entries **下标**，注释声称「下标仅追加不删，稳定」，
 *    但 `projectTurnTimeline` 里 `selectVisibleEntries` 会在末尾 push pending 块——
 *    此处只读不改下标，安全；真正的风险在 `flushPending` 在 tool 事件前插入 entries，
 *    验证下标在插入后仍指向正确条目。
 * 3. `final_response` 的 `hasDeltaStreamed` 判定跨 turn 复用 EMPTY_PROJECTION_STATE 时
 *    是否被污染（共享常量对象作为初始状态）。
 *
 * @module tests/projector.ordering
 */

import { describe, expect, it, vi } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import {
  EMPTY_PROJECTION_STATE,
  createTimelineProjectorState,
  projectTimelineIncrementally,
  projectTurnTimeline,
  type TurnTimelineEntry,
} from "@/services/timeline/projector";

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

let seq = 0;

/** 构造事件。 */
function makeEvent(
  eventType: string,
  payload: Record<string, unknown>,
  turnId = "turn-1",
): RuntimeEvent {
  seq += 1;
  return {
    event_id: `evt-${seq}`,
    task_id: "task-1",
    turn_id: turnId,
    event_type: eventType,
    payload,
    created_at: new Date().toISOString(),
  } as unknown as RuntimeEvent;
}

/** 取出所有工具条目。 */
function toolItems(entries: TurnTimelineEntry[]) {
  return entries
    .filter((e): e is Extract<TurnTimelineEntry, { kind: "tool" }> => e.kind === "tool")
    .map((e) => e.item);
}

describe("projector 排序与合并", () => {
  it("flushPending 在工具事件前插入条目后，callId 下标仍指向正确的工具条目", () => {
    const callId = "c1";
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("tool_call_started", { tool_name: "read_file", tool_call_id: callId, arguments: { path: "a.ts" } }),
    ]);
    // 工具条目在 index 0
    state = projectTimelineIncrementally(state, [
      makeEvent("model_output_delta", { text: "hi" }),
      makeEvent("tool_call_finished", {
        tool_name: "read_file",
        tool_call_id: callId,
        status: "success",
        content: "file body",
      }),
    ]);
    const tools = toolItems(state.entries);
    expect(tools).toHaveLength(1);
    expect(tools[0].result).toBe("file body");
    expect(tools[0].status).toBe("completed");
  });

  it("finished 合并时应保留 started 提供的 arguments", () => {
    const callId = "c2";
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("tool_call_started", { tool_name: "read_file", tool_call_id: callId, arguments: { path: "a.ts" } }),
    ]);
    state = projectTimelineIncrementally(state, [
      makeEvent("tool_call_finished", { tool_name: "read_file", tool_call_id: callId, status: "success", content: "x" }),
    ]);
    expect(toolItems(state.entries)[0].arguments).toEqual({ path: "a.ts" });
  });

  it("tool_call_finished 先到（无 started）时不应被后到的 started 覆盖成 running", () => {
    const callId = "c3";
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("tool_call_finished", { tool_name: "read_file", tool_call_id: callId, status: "success", content: "x" }),
    ]);
    state = projectTimelineIncrementally(state, [
      makeEvent("tool_call_started", { tool_name: "read_file", tool_call_id: callId, arguments: { path: "a.ts" } }),
    ]);
    const tools = toolItems(state.entries);
    expect(tools).toHaveLength(1);
    expect(tools[0].status).toBe("completed");
  });

  it("projectTurnTimeline 多轮共享 EMPTY_PROJECTION_STATE 不产生跨 turn 污染", () => {
    const turns = [
      { turn_id: "turn-1", task_id: "task-1", input_text: "q1", response_text: null },
      { turn_id: "turn-2", task_id: "task-1", input_text: "q2", response_text: null },
    ] as unknown as TurnRecord[];
    const events = [
      makeEvent("model_output_delta", { text: "A" }, "turn-1"),
      makeEvent("model_output_delta", { text: "B" }, "turn-2"),
    ];
    const result = projectTurnTimeline(turns, events);
    const t1 = result[0].entries.map((e) => (e.kind === "assistant" ? e.content : ""));
    const t2 = result[1].entries.map((e) => (e.kind === "assistant" ? e.content : ""));
    expect(t1).toEqual(["A"]);
    expect(t2).toEqual(["B"]);
    // 共享常量不得被写坏
    expect(EMPTY_PROJECTION_STATE.entries).toHaveLength(0);
    expect(EMPTY_PROJECTION_STATE.processedEventIds.size).toBe(0);
    expect(EMPTY_PROJECTION_STATE.toolByCallId.size).toBe(0);
  });

  it("final_response 在有 delta 流过时不重复追加正文", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("model_output_delta", { text: "hello" }),
      makeEvent("final_response", { text: "hello" }),
    ]);
    const assistants = state.entries.filter((e) => e.kind === "assistant");
    expect(assistants).toHaveLength(1);
  });
});
