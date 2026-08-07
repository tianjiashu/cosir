/**
 * 事件分片（eventsByTaskId / eventsByTurnId）顺序一致性缺陷验证测试。
 *
 * 背景：`appendEvent` / `appendEvents` 对**扁平 `events`** 做了乱序检测与整体排序
 * （`needsReorder` / `incomingOutOfOrder` + `compareRuntimeEvents`），
 * 但对 `eventsByTaskId` / `eventsByTurnId` 两个**分片**只做无条件 `concat`，永不排序。
 *
 * 而真实渲染链路（ChatPanel → eventsByTurnId[turn_id] → TurnTimeline → projector）
 * **只消费分片，从不消费扁平 `events`**（扁平数组在 ChatPanel 仅用于 `events.length` 触发滚动）。
 *
 * 因此本测试验证：排序保护是否覆盖了真正被渲染消费的数据源。
 *
 * @module tests/eventStore.shardOrdering
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import { useEventStore, selectEventsForTask } from "@/stores/eventStore";

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

/**
 * 构造运行时事件。
 *
 * @param id - event_id。
 * @param sequence - 后端全局序号（决定正确顺序）。
 * @param taskId - 所属任务。
 * @param turnId - 所属轮次。
 * @returns RuntimeEvent。
 */
function ev(
  id: string,
  sequence: number,
  taskId = "task-1",
  turnId = "turn-1",
): RuntimeEvent {
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

/** 读取指定 turn 分片的 sequence 序列。 */
function turnSeqs(turnId = "turn-1"): number[] {
  const shard = useEventStore.getState().eventsByTurnId[turnId] ?? [];
  return shard.map((e) => (e as unknown as { sequence: number }).sequence);
}

/** 读取指定 task 分片的 sequence 序列。 */
function taskSeqs(taskId = "task-1"): number[] {
  const shard = selectEventsForTask(useEventStore.getState(), taskId);
  return shard.map((e) => (e as unknown as { sequence: number }).sequence);
}

/** 读取扁平 events 的 sequence 序列。 */
function flatSeqs(): number[] {
  return useEventStore
    .getState()
    .events.map((e) => (e as unknown as { sequence: number }).sequence);
}

describe("eventStore 分片顺序一致性", () => {
  beforeEach(() => {
    useEventStore.getState().clearEvents();
  });

  it("appendEvents 批内乱序时，turn 分片应与扁平 events 顺序一致", () => {
    const { appendEvents } = useEventStore.getState();
    // 同一批内乱序到达（SSE 攒批 / 网络重排）
    appendEvents([ev("e2", 2), ev("e1", 1)]);

    // 扁平数组与分片经 appendOrderedShard 逐条归位，已排序
    expect(flatSeqs()).toEqual([1, 2]);
    // 渲染真正消费的 turn 分片是否同样有序？
    expect(turnSeqs()).toEqual([1, 2]);
  });

  it("appendEvents 跨批逆序到达时，turn 分片应与扁平 events 顺序一致", () => {
    const { appendEvents } = useEventStore.getState();
    // 先到 seq=5，随后迟到 seq=3（跨批逆序，命中 needsReorder 分支）
    appendEvents([ev("e5", 5)]);
    appendEvents([ev("e3", 3)]);

    expect(flatSeqs()).toEqual([3, 5]);
    expect(turnSeqs()).toEqual([3, 5]);
  });

  it("appendEvent 单条逆序到达时，turn 分片应与扁平 events 顺序一致", () => {
    const { appendEvent } = useEventStore.getState();
    appendEvent(ev("e5", 5));
    appendEvent(ev("e3", 3));

    expect(flatSeqs()).toEqual([3, 5]);
    expect(turnSeqs()).toEqual([3, 5]);
  });

  it("task 分片在逆序到达时也应保持 sequence 有序", () => {
    const { appendEvents } = useEventStore.getState();
    appendEvents([ev("e9", 9)]);
    appendEvents([ev("e4", 4)]);

    expect(flatSeqs()).toEqual([4, 9]);
    expect(taskSeqs()).toEqual([4, 9]);
  });

  it("setEvents（历史回放）与 appendEvents（实时）叠加后分片仍应有序", () => {
    const store = useEventStore.getState();
    // 实时先收到较大 sequence
    store.appendEvents([ev("e8", 8)]);
    // 历史回放补齐更早的事件
    store.setEvents([ev("e2", 2), ev("e5", 5)], "task-1");

    // setEvents 内部 mergeByEventId 会排序，这里作为对照：分片应有序
    // （appendEvents 的实时追加与 setEvents 的历史回放共用 compareRuntimeEvents 同口径）
    expect(turnSeqs()).toEqual([2, 5, 8]);
  });
});
