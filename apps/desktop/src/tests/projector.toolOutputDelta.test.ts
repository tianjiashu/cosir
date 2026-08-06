/**
 * timeline 投影器「终端输出实时增量」分支测试。
 *
 * 覆盖 `tool_output_delta` 事件的投影语义：按 tool_call_id 累积到既有工具条目、
 * 孤立 delta 丢弃、进入终态后清空运行期 output、以及重复事件的幂等性。
 *
 * @module tests/projector.toolOutputDelta
 */

import { describe, expect, it } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import {
  createTimelineProjectorState,
  projectTimelineIncrementally,
  type TurnTimelineEntry,
} from "@/services/timeline/projector";

/** 事件序号计数器，保证每个构造事件拥有唯一 event_id。 */
let eventSeq = 0;

/**
 * 构造一个最小可用的 RuntimeEvent。
 *
 * 参数:
 *   eventType - 事件类型。
 *   payload - 事件载荷。
 *   eventId - 可选事件 ID；缺省时自动生成唯一 ID。
 *
 * 返回:
 *   可直接投喂投影器的 RuntimeEvent。
 */
function makeEvent(
  eventType: RuntimeEvent["event_type"],
  payload: Record<string, unknown>,
  eventId?: string,
): RuntimeEvent {
  eventSeq += 1;
  return {
    event_id: eventId ?? `evt-${eventSeq}`,
    task_id: "task-1",
    turn_id: "turn-1",
    event_type: eventType,
    payload,
    created_at: new Date().toISOString(),
  } as RuntimeEvent;
}

/**
 * 从投影条目中取出唯一的工具条目。
 *
 * 参数:
 *   entries - 投影后的 timeline 条目。
 *
 * 返回:
 *   首个工具条目的 item。
 *
 * 异常:
 *   若不存在工具条目，断言失败。
 */
function firstToolItem(entries: TurnTimelineEntry[]) {
  const entry = entries.find((e): e is Extract<TurnTimelineEntry, { kind: "tool" }> => e.kind === "tool");
  expect(entry).toBeDefined();
  return entry!.item;
}

/** 构造一个 execute_terminal 的 tool_call_started 事件。 */
function startedEvent(callId: string): RuntimeEvent {
  return makeEvent("tool_call_started", {
    tool_name: "execute_terminal",
    tool_call_id: callId,
    step_id: "step-1",
    arguments: { command: "echo hi" },
  });
}

describe("projector tool_output_delta", () => {
  it("按 tool_call_id 累积多次输出增量", () => {
    const callId = "call-1";
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      startedEvent(callId),
      makeEvent("tool_output_delta", { tool_call_id: callId, step_id: "step-1", text: "line1\n" }),
      makeEvent("tool_output_delta", { tool_call_id: callId, step_id: "step-1", text: "line2\n" }),
    ]);

    const item = firstToolItem(state.entries);
    expect(item.status).toBe("running");
    expect(item.output).toBe("line1\nline2\n");
  });

  it("找不到对应 started 条目时丢弃增量，不新建孤立条目", () => {
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      makeEvent("tool_output_delta", { tool_call_id: "unknown", step_id: "step-1", text: "orphan" }),
    ]);

    expect(state.entries).toHaveLength(0);
  });

  it("重复 event_id 的增量只累积一次（幂等）", () => {
    const callId = "call-2";
    const delta = makeEvent(
      "tool_output_delta",
      { tool_call_id: callId, step_id: "step-1", text: "once" },
      "evt-dup",
    );
    let state = projectTimelineIncrementally(createTimelineProjectorState(), [startedEvent(callId), delta]);
    state = projectTimelineIncrementally(state, [delta]);

    expect(firstToolItem(state.entries).output).toBe("once");
  });

  it("进入终态后清空运行期 output，改由 result 承载", () => {
    const callId = "call-3";
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      startedEvent(callId),
      makeEvent("tool_output_delta", { tool_call_id: callId, step_id: "step-1", text: "streaming" }),
      makeEvent("tool_call_finished", {
        tool_name: "execute_terminal",
        tool_call_id: callId,
        step_id: "step-1",
        status: "success",
        content: "final output",
      }),
    ]);

    const item = firstToolItem(state.entries);
    expect(item.status).toBe("completed");
    expect(item.output).toBeUndefined();
    expect(item.result).toBe("final output");
  });

  it("刷新重连：重放流不含 delta 时输出全部由 result 承载", () => {
    // tool_output_delta 是不持久化的实时广播，刷新后重放的 runtime_events 中不存在；
    // 此用例固化该契约，防止后续误改成持久化或让 output 承接终态输出。
    const callId = "call-5";
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      startedEvent(callId),
      makeEvent("tool_call_finished", {
        tool_name: "execute_terminal",
        tool_call_id: callId,
        step_id: "step-1",
        status: "success",
        content: "replayed output",
      }),
    ]);

    const item = firstToolItem(state.entries);
    expect(item.output).toBeUndefined();
    expect(item.result).toBe("replayed output");
  });

  it("增量不打断进行中的 assistant 文本块", () => {
    const callId = "call-4";
    const state = projectTimelineIncrementally(createTimelineProjectorState(), [
      startedEvent(callId),
      makeEvent("model_output_delta", { text: "he" }),
      makeEvent("tool_output_delta", { tool_call_id: callId, step_id: "step-1", text: "out" }),
      makeEvent("model_output_delta", { text: "llo" }),
    ]);

    expect(state.pendingDelta?.content).toBe("hello");
    expect(firstToolItem(state.entries).output).toBe("out");
  });
});
