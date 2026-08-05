import { beforeEach, describe, expect, it } from "vitest";
import { useEventStore, EMPTY_EVENTS } from "@/stores/eventStore";
import { SSEConnectionState } from "@/services/sse";
import type { RuntimeEvent } from "@shared/events";

function makeEvent(
  eventId: string,
  turnId: string,
  taskId: string,
  sequence: number,
): RuntimeEvent {
  return {
    event_id: eventId,
    task_id: taskId,
    turn_id: turnId,
    event_type: "model_output_delta",
    sequence,
    created_at: new Date(sequence * 1000).toISOString(),
    payload: { text: `t-${eventId}` },
  } as unknown as RuntimeEvent;
}

describe("eventStore.setEvents 引用稳定性（历史加载不击穿 memo）", () => {
  beforeEach(() => {
    // 重置 store 到干净初始态
    useEventStore.setState({
      events: EMPTY_EVENTS,
      eventsByTaskId: {},
      eventsByTurnId: {},
      connectionState: SSEConnectionState.IDLE,
      processedEventIds: new Set<string>(),
    });
  });

  it("回放新增其它轮次的事件时，未变化轮次的 events 数组引用保持不变", () => {
    const TASK = "task-1";
    const eventsTurnA = [
      makeEvent("a1", "turn-a", TASK, 1),
      makeEvent("a2", "turn-a", TASK, 2),
    ];
    const eventsTurnB = [makeEvent("b1", "turn-b", TASK, 3)];

    // 首次打开任务：灌入 turn-a、turn-b 历史
    useEventStore.getState().setEvents([...eventsTurnA, ...eventsTurnB], TASK);
    const refA1 = useEventStore.getState().eventsByTurnId["turn-a"];
    const refB1 = useEventStore.getState().eventsByTurnId["turn-b"];
    expect(refA1).not.toBeUndefined();
    expect(refB1).not.toBeUndefined();

    // 再次回放：新增 turn-c 的历史（与已加载的 turn-a/turn-b 无交集）
    const eventsTurnC = [makeEvent("c1", "turn-c", TASK, 4)];
    useEventStore.getState().setEvents(eventsTurnC, TASK);

    const refA2 = useEventStore.getState().eventsByTurnId["turn-a"];
    const refB2 = useEventStore.getState().eventsByTurnId["turn-b"];
    // 关键不变量：turn-a / turn-b 内容未变 → 数组引用必须复用，
    // 否则下游 TurnTimeline 的 memo 会被击穿，历史加载时整棵 timeline 重投影卡顿。
    expect(refA2).toBe(refA1);
    expect(refB2).toBe(refB1);
    // 新增的 turn-c 应有独立且正确的引用。
    expect(useEventStore.getState().eventsByTurnId["turn-c"]).toHaveLength(1);
  });

  it("同一轮次事件引用未变时，引用保持（mergeByEventId 语义防线）", () => {
    const TASK = "task-2";
    const events = [makeEvent("x1", "turn-x", TASK, 1), makeEvent("x2", "turn-x", TASK, 2)];
    useEventStore.getState().setEvents(events, TASK);
    const ref1 = useEventStore.getState().eventsByTurnId["turn-x"];

    // 注意：此处用同一批对象引用（[...events] 仅展开数组、元素引用不变）模拟「已缓存对象被
    // 再次传入」的场景。真实冷启动路径（api.listTaskEvents 返回新反序列化对象）下引用必不同，
    // 此时引用稳定的真正来源是 useTask.openTask 的缓存命中跳过 setEvents，而非 mergeByEventId；
    // mergeByEventId 的引用复用仅作为「同引用幂等合并」的语义防线存在。本用例验证该防线成立。
    useEventStore.getState().setEvents([...events], TASK);
    const ref2 = useEventStore.getState().eventsByTurnId["turn-x"];
    expect(ref2).toBe(ref1);
  });

  it("某轮次事件更新时引用必须变化（不错误地复用旧引用）", () => {
    const TASK = "task-3";
    const original = makeEvent("y1", "turn-y", TASK, 1);
    useEventStore.getState().setEvents([original], TASK);
    const ref1 = useEventStore.getState().eventsByTurnId["turn-y"];

    // 同一 event_id 但内容变化（新对象引用）→ mergeByEventId 应返回新数组
    const updated = { ...original, payload: { text: "changed" } } as RuntimeEvent;
    useEventStore.getState().setEvents([updated], TASK);
    const ref2 = useEventStore.getState().eventsByTurnId["turn-y"];
    expect(ref2).not.toBe(ref1);
    expect(ref2[0].payload).toEqual({ text: "changed" });
  });
});
