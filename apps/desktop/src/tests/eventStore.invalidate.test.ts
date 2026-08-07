/**
 * eventStore 缺陷验证测试。
 *
 * 覆盖三处疑似缺陷：
 * 1. `invalidateTask` 用 turn_id 反查删除时，若同一 turn_id 被跨 task 复用（理论上不该发生，
 *    但更现实的问题是：`events` 扁平数组按 task 过滤后与分片不一致）。
 * 2. `appendEvent` / `appendEvents` 假设「后端 sequence 全局单调」直接 push，
 *    乱序到达时扁平数组顺序错乱，而 `setEvents` 却会排序 —— 两条写入路径顺序语义不一致。
 * 3. `setEvents` 用传入的全量事件**替换** `events`，会丢弃其它任务在扁平数组中的事件，
 *    而 `eventsByTaskId` 却是合并保留 —— 同一 store 内两份数据自相矛盾。
 *
 * @module tests/eventStore.invalidate
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import { useEventStore } from "@/stores/eventStore";

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

/**
 * 构造事件。
 *
 * @param id - event_id。
 * @param taskId - 所属任务。
 * @param turnId - 所属轮次。
 * @param sequence - 后端序号。
 * @returns RuntimeEvent。
 */
function ev(id: string, taskId: string, turnId: string | null, sequence: number): RuntimeEvent {
  return {
    event_id: id,
    task_id: taskId,
    turn_id: turnId,
    event_type: "model_output_delta",
    payload: { text: id },
    sequence,
    created_at: new Date(1700000000000 + sequence * 1000).toISOString(),
  } as unknown as RuntimeEvent;
}

describe("eventStore", () => {
  beforeEach(() => {
    useEventStore.getState().clearEvents();
  });

  it("appendEvents 乱序到达时扁平 events 顺序应与 sequence 一致", () => {
    const { appendEvents } = useEventStore.getState();
    // 后端 sequence 单调只是「产生顺序」；SSE 攒批 + 历史回放叠加时到达顺序可能乱。
    appendEvents([ev("e2", "t1", "turn1", 2), ev("e1", "t1", "turn1", 1)]);
    const seqs = useEventStore.getState().events.map((e) => (e as unknown as { sequence: number }).sequence);
    expect(seqs).toEqual([1, 2]);
  });

  it("setEvents 不应清空其它任务在扁平 events 中的事件", () => {
    const store = useEventStore.getState();
    store.setEvents([ev("a1", "taskA", "turnA", 1)], "taskA");
    store.setEvents([ev("b1", "taskB", "turnB", 1)], "taskB");

    const state = useEventStore.getState();
    // 分片保留了两个任务
    expect(Object.keys(state.eventsByTaskId).sort()).toEqual(["taskA", "taskB"]);
    // 扁平数组却只剩最后一次传入的 taskB —— 与分片自相矛盾
    const taskIds = new Set(state.events.map((e) => e.task_id));
    expect([...taskIds].sort()).toEqual(["taskA", "taskB"]);
  });

  it("invalidateTask 后去重集合回收，被删事件可重新写入", () => {
    const store = useEventStore.getState();
    store.appendEvents([ev("a1", "taskA", "turnA", 1)]);
    store.invalidateTask("taskA");
    store.appendEvents([ev("a1", "taskA", "turnA", 1)]);
    expect(useEventStore.getState().events).toHaveLength(1);
  });
});
