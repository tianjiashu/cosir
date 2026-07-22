import { describe, expect, it } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import type { TurnRecord } from "@shared/turn";
import { projectTurnTimeline } from "@/services/timeline/projector";

function makeTurn(turnId: string, inputText: string): TurnRecord {
  const now = new Date().toISOString();
  return {
    turn_id: turnId,
    task_id: "task-1",
    input_text: inputText,
    status: "running",
    created_at: now,
    updated_at: now,
  } as TurnRecord;
}

function makeDelta(eventId: string, text: string, sequence = 1): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: "model_output_delta",
    task_id: "task-1",
    turn_id: "turn-1",
    sequence,
    created_at: new Date().toISOString(),
    payload: { text },
  };
}

function makeThinking(eventId: string, text: string, sequence = 1): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: "model_thinking_delta",
    task_id: "task-1",
    turn_id: "turn-1",
    sequence,
    created_at: new Date().toISOString(),
    payload: { text },
  };
}

function makeDeltaWithPayload(eventId: string, payload: Record<string, unknown>, sequence = 1): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: "model_output_delta",
    task_id: "task-1",
    turn_id: "turn-1",
    sequence,
    created_at: new Date().toISOString(),
    payload,
  };
}

function makeToolRequested(eventId: string, toolName: string, sequence = 1): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: "tool_call_requested",
    task_id: "task-1",
    turn_id: "turn-1",
    sequence,
    created_at: new Date().toISOString(),
    payload: { tool_name: toolName, arguments: {} },
  };
}

function makeToolCallFinished(eventId: string, toolName: string, status: string, sequence = 1): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: "tool_call_finished",
    task_id: "task-1",
    turn_id: "turn-1",
    sequence,
    created_at: new Date().toISOString(),
    payload: { tool_name: toolName, status },
  };
}

function makeRunFinished(eventId: string, sequence = 1): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: "run_finished",
    task_id: "task-1",
    turn_id: "turn-1",
    sequence,
    created_at: new Date().toISOString(),
    payload: { status: "completed" },
  };
}

function makeRunFailed(eventId: string, error: string, sequence = 1): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: "run_failed",
    task_id: "task-1",
    turn_id: "turn-1",
    sequence,
    created_at: new Date().toISOString(),
    payload: { status: "failed", error },
  };
}

function makeRunCancelled(eventId: string, sequence = 1): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: "run_cancelled",
    task_id: "task-1",
    turn_id: "turn-1",
    sequence,
    created_at: new Date().toISOString(),
    payload: { status: "cancelled" },
  };
}

function makeUnknownEvent(eventId: string, eventType: RuntimeEvent["event_type"], sequence = 1): RuntimeEvent {
  return {
    event_id: eventId,
    event_type: eventType,
    task_id: "task-1",
    turn_id: "turn-1",
    sequence,
    created_at: new Date().toISOString(),
    payload: {},
  };
}

describe("timeline projector", () => {
  it("相邻 model_output_delta 聚合为一条 assistant 消息", () => {
    const turn = makeTurn("turn-1", "hello");
    const events = [makeDelta("e-1", "我来"), makeDelta("e-2", "查看"), makeDelta("e-3", "当前项目。")];
    const timeline = projectTurnTimeline([turn], events);

    expect(timeline).toHaveLength(1);
    expect(timeline[0].entries).toHaveLength(1);
    expect(timeline[0].entries[0]).toEqual({
      kind: "assistant",
      eventId: "e-1",
      content: "我来查看当前项目。",
    });
  });

  it("delta 被 tool 事件中断时拆分为两条 assistant 消息", () => {
    const turn = makeTurn("turn-1", "hello");
    const events = [
      makeDelta("e-1", "我来"),
      makeDelta("e-2", "查看"),
      makeToolRequested("e-3", "read_file"),
      makeDelta("e-4", "继续输出"),
    ];
    const timeline = projectTurnTimeline([turn], events);

    expect(timeline[0].entries).toHaveLength(3);
    expect(timeline[0].entries[0]).toEqual({ kind: "assistant", eventId: "e-1", content: "我来查看" });
    expect(timeline[0].entries[1].kind).toBe("tool");
    expect(timeline[0].entries[2]).toEqual({ kind: "assistant", eventId: "e-4", content: "继续输出" });
  });

  it("delta 被 status 事件中断时拆分为两条 assistant 消息", () => {
    const turn = makeTurn("turn-1", "hello");
    const events = [makeDelta("e-1", "第一部"), makeRunFinished("e-2"), makeDelta("e-3", "第二部")];
    const timeline = projectTurnTimeline([turn], events);

    expect(timeline[0].entries).toHaveLength(3);
    expect(timeline[0].entries[0]).toEqual({ kind: "assistant", eventId: "e-1", content: "第一部" });
    expect(timeline[0].entries[1].kind).toBe("status");
    expect(timeline[0].entries[2]).toEqual({ kind: "assistant", eventId: "e-3", content: "第二部" });
  });

  it("无事件时返回空 entries", () => {
    const turn = makeTurn("turn-1", "hello");
    const timeline = projectTurnTimeline([turn], []);

    expect(timeline[0].entries).toHaveLength(0);
    expect(timeline[0].userText).toBe("hello");
  });

  it("delta payload 为 null/undefined 时按空字符串聚合", () => {
    const turn = makeTurn("turn-1", "hello");
    const events = [
      makeDeltaWithPayload("e-1", { text: null }),
      makeDeltaWithPayload("e-2", { text: undefined }),
      makeDelta("e-3", "后续"),
    ];
    const timeline = projectTurnTimeline([turn], events);

    expect(timeline[0].entries).toHaveLength(1);
    expect(timeline[0].entries[0]).toEqual({
      kind: "assistant",
      eventId: "e-1",
      content: "后续",
    });
  });

  it("未知事件类型被忽略但会中断相邻 delta 聚合", () => {
    const turn = makeTurn("turn-1", "hello");
    const events = [
      makeDelta("e-1", "前缀"),
      makeUnknownEvent("e-2", "run_started"),
      makeUnknownEvent("e-3", "step_started"),
      makeDelta("e-4", "后缀"),
    ];
    const timeline = projectTurnTimeline([turn], events);

    // 当前实现：任何非 delta 事件都会先 flush pendingDelta，因此未知事件虽自身不产出条目，
    // 但会把前后 delta 拆成两条 assistant 消息。
    expect(timeline[0].entries).toHaveLength(2);
    expect(timeline[0].entries[0]).toEqual({ kind: "assistant", eventId: "e-1", content: "前缀" });
    expect(timeline[0].entries[1]).toEqual({ kind: "assistant", eventId: "e-4", content: "后缀" });
  });

  it("相邻 model_thinking_delta 聚合为一条 thinking 条目，且显示在回答之前", () => {
    const turn = makeTurn("turn-1", "hello");
    const events = [
      makeThinking("t-1", "先分析"),
      makeThinking("t-2", "需求"),
      makeDelta("e-1", "我来帮你"),
    ];
    const timeline = projectTurnTimeline([turn], events);

    expect(timeline[0].entries).toHaveLength(2);
    expect(timeline[0].entries[0]).toEqual({ kind: "thinking", eventId: "t-1", content: "先分析需求" });
    expect(timeline[0].entries[1]).toEqual({ kind: "assistant", eventId: "e-1", content: "我来帮你" });
  });

  it("按 turn_id 隔离事件，不同 turn 互不干扰", () => {
    const turnA = makeTurn("turn-a", "hello A");
    const turnB = makeTurn("turn-b", "hello B");
    const events: RuntimeEvent[] = [
      {
        event_id: "e-1",
        event_type: "model_output_delta",
        task_id: "task-1",
        turn_id: "turn-a",
        sequence: 1,
        created_at: new Date().toISOString(),
        payload: { text: "A 的内容" },
      },
      {
        event_id: "e-2",
        event_type: "model_output_delta",
        task_id: "task-1",
        turn_id: "turn-b",
        sequence: 2,
        created_at: new Date().toISOString(),
        payload: { text: "B 的内容" },
      },
    ];
    const timeline = projectTurnTimeline([turnA, turnB], events);

    expect(timeline).toHaveLength(2);
    expect(timeline[0].entries).toHaveLength(1);
    expect(timeline[0].entries[0]).toEqual({ kind: "assistant", eventId: "e-1", content: "A 的内容" });
    expect(timeline[1].entries).toHaveLength(1);
    expect(timeline[1].entries[0]).toEqual({ kind: "assistant", eventId: "e-2", content: "B 的内容" });
  });

  it("run_failed 和 run_cancelled 投影为 status 条目", () => {
    const turn = makeTurn("turn-1", "hello");
    const events = [makeRunFailed("e-1", "something went wrong"), makeRunCancelled("e-2")];
    const timeline = projectTurnTimeline([turn], events);

    expect(timeline[0].entries).toHaveLength(2);
    expect(timeline[0].entries[0]).toEqual({
      kind: "status",
      eventId: "e-1",
      eventType: "run_failed",
      payload: { status: "failed", error: "something went wrong" },
    });
    expect(timeline[0].entries[1]).toEqual({
      kind: "status",
      eventId: "e-2",
      eventType: "run_cancelled",
      payload: { status: "cancelled" },
    });
  });

  it("tool_call_finished 投影为 tool 条目", () => {
    const turn = makeTurn("turn-1", "hello");
    const events = [
      makeToolCallFinished("e-2", "read_file", "completed"),
      makeToolCallFinished("e-3", "shell", "error"),
    ];
    const timeline = projectTurnTimeline([turn], events);

    expect(timeline[0].entries).toHaveLength(2);
    expect(timeline[0].entries[0]).toEqual({
      kind: "tool",
      item: {
        eventId: "e-2",
        toolName: "read_file",
        status: "completed",
      },
    });
    expect(timeline[0].entries[1]).toEqual({
      kind: "tool",
      item: {
        eventId: "e-3",
        toolName: "shell",
        status: "error",
      },
    });
  });
});
