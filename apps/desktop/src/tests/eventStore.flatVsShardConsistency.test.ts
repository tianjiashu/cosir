/**
 * eventStore 扁平 `events` 与 `eventsByTaskId` 分片一致性缺陷验证。
 *
 * 背景：`setEvents` 内两条路径口径矛盾：
 * - 分片 `mergedByTask[taskId] = mergeByEventId(旧, sorted)` —— **合并**（旧+新都保留）
 * - 扁平 `mergedEvents = filter(task_id!==taskId) + sorted` —— **全量覆盖为传入集**
 *
 * 当调用方以「增量追加」方式调用 `setEvents`（只传该 task 的新增子集，而非全量历史）时，
 * 扁平数组会丢弃同 task 之前缓存的事件，而分片保留 → 同一 store 内两份数据自相矛盾。
 *
 * 真实触发：`useChanges.incremental.test.ts` 中「先 setEvents(200条历史) 再
 * setEvents(1条新增)」的增量模式；以及 useTask 冷启动后 SSE 增量并入场景。
 *
 * 影响：`selectEventCount`（消费扁平 events）会少算；虽 UI 渲染主要走分片
 * （selectEventsForTask），但数据不一致是隐患，可能影响依赖 events.length 的逻辑。
 *
 * @module tests/eventStore.flatVsShardConsistency
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeEvent } from "@shared/events";
import { useEventStore } from "@/stores/eventStore";

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

function ev(id: string, taskId: string, turnId: string, sequence: number): RuntimeEvent {
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

describe("eventStore 扁平 events 与分片一致性", () => {
  beforeEach(() => {
    useEventStore.getState().clearEvents();
  });

  it("同一 task 增量追加时，扁平 events 不应丢失之前缓存的事件（数据一致性）", () => {
    const store = useEventStore.getState();
    // 首次：灌入该 task 的完整历史（100 条）
    const history = Array.from({ length: 100 }, (_, i) => ev(`h-${i}`, "taskA", "turnA", i + 1));
    store.setEvents(history, "taskA");
    expect(useEventStore.getState().events).toHaveLength(100);

    // 增量追加 1 条新增（模拟历史加载后 SSE 又到一条、走 setEvents 增量并入）
    const inc = ev("h-100", "taskA", "turnA", 101);
    store.setEvents([inc], "taskA");

    const state = useEventStore.getState();
    // 分片：合并保留，应 101 条
    expect(state.eventsByTaskId["taskA"]).toHaveLength(101);
    // 扁平：应同样 101 条（与分片一致）
    expect(state.events).toHaveLength(101);
    // 扁平不得丢失 h-0..h-99
    const flatIds = new Set(state.events.map((e) => e.event_id));
    expect(flatIds.has("h-0")).toBe(true);
    expect(flatIds.has("h-99")).toBe(true);
  });

  it("selectEventCount 与分片长度一致（扁平数组丢失的直接表现）", () => {
    const store = useEventStore.getState();
    store.setEvents(
      [ev("a1", "taskA", "turnA", 1), ev("a2", "taskA", "turnA", 2)],
      "taskA",
    );
    store.setEvents([ev("a3", "taskA", "turnA", 3)], "taskA");

    const state = useEventStore.getState();
    // 分片是权威：3 条
    expect(state.eventsByTaskId["taskA"]).toHaveLength(3);
    // selectEventCount 消费扁平，若丢失则为 1，与分片 3 矛盾
    expect(state.events.length).toBe(state.eventsByTaskId["taskA"].length);
  });

  it("appendEvents 增量路径（同 task）不应与分片漂移", () => {
    const store = useEventStore.getState();
    store.setEvents([ev("a1", "taskA", "turnA", 1), ev("a2", "taskA", "turnA", 2)], "taskA");
    store.appendEvents([ev("a3", "taskA", "turnA", 3)]);
    // 分片与扁平都应 3 条且一致
    const state = useEventStore.getState();
    expect(state.eventsByTaskId["taskA"]).toHaveLength(3);
    expect(state.events).toHaveLength(3);
  });
});
