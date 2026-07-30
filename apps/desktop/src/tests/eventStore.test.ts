import { describe, it, expect, beforeEach } from "vitest";
import { useEventStore, selectLatestEvent, selectEventCount } from "@/stores/eventStore";
import { SSEConnectionState } from "@/services/sse";
import { makeEvent } from "@/tests/test-utils/factories";

beforeEach(() => {
  useEventStore.getState().clearEvents();
});

describe("eventStore — 回放去重与状态", () => {
  it("appendEvent 追加新事件", () => {
    const s = useEventStore.getState();
    s.appendEvent(makeEvent("e1"));
    expect(useEventStore.getState().events).toHaveLength(1);
    expect(useEventStore.getState().processedEventIds.has("e1")).toBe(true);
  });

  it("appendEvent 同 event_id 回放去重（跳过）", () => {
    const s = useEventStore.getState();
    s.appendEvent(makeEvent("e1"));
    s.appendEvent(makeEvent("e1")); // 重复
    expect(useEventStore.getState().events).toHaveLength(1);
  });

  it("setEvents 批量替换并重建去重集合", () => {
    useEventStore.getState().setEvents([makeEvent("a"), makeEvent("b")]);
    const st = useEventStore.getState();
    expect(st.events).toHaveLength(2);
    expect(st.processedEventIds.has("a")).toBe(true);
    expect(st.processedEventIds.has("b")).toBe(true);
    // 重复 id 不再追加
    st.appendEvent(makeEvent("a"));
    expect(useEventStore.getState().events).toHaveLength(2);
  });

  it("setConnectionState 更新状态为枚举成员（非字符串字面量）", () => {
    useEventStore.getState().setConnectionState(SSEConnectionState.STREAMING);
    expect(useEventStore.getState().connectionState).toBe(SSEConnectionState.STREAMING);
    expect(useEventStore.getState().connectionState).toBe("streaming");
  });

  it("clearEvents 后 events 清空且 connectionState 回到 IDLE（枚举成员）", () => {
    const s = useEventStore.getState();
    s.appendEvent(makeEvent("e1"));
    s.setConnectionState(SSEConnectionState.STREAMING);
    s.clearEvents();
    const st = useEventStore.getState();
    expect(st.events).toHaveLength(0);
    expect(st.processedEventIds.size).toBe(0);
    expect(st.connectionState).toBe(SSEConnectionState.IDLE);
    expect(st.connectionState).toBe("idle");
  });

  it("selectLatestEvent 返回指定任务最后一条", () => {
    useEventStore.getState().setEvents([makeEvent("a"), makeEvent("b")]);
    expect(selectLatestEvent(useEventStore.getState(), "task-1")?.event_id).toBe("b");
  });

  it("selectEventCount 返回数量", () => {
    useEventStore.getState().setEvents([makeEvent("a"), makeEvent("b"), makeEvent("c")]);
    expect(selectEventCount(useEventStore.getState())).toBe(3);
  });

  it("空集合或任务无缓存时 selectLatestEvent 返回 undefined", () => {
    expect(selectLatestEvent(useEventStore.getState(), "task-1")).toBeUndefined();
  });

  it("setEvents 合并保留其他任务缓存（跨任务切换不重复拉取）", () => {
    // 打开任务 A：灌入历史事件
    useEventStore.getState().setEvents([makeEvent("a1", { task_id: "A" })], "A");
    // 打开任务 B：A 的缓存应保留
    useEventStore.getState().setEvents([makeEvent("b1", { task_id: "B" })], "B");
    expect(useEventStore.getState().eventsByTaskId["A"]).toHaveLength(1);
    expect(useEventStore.getState().eventsByTaskId["B"]).toHaveLength(1);
    // 再次打开 A：合并去重，且不丢失 B 的缓存
    useEventStore.getState().setEvents(
      [makeEvent("a1", { task_id: "A" }), makeEvent("a2", { task_id: "A" })],
      "A",
    );
    expect(useEventStore.getState().eventsByTaskId["A"]).toHaveLength(2);
    expect(useEventStore.getState().eventsByTaskId["B"]).toHaveLength(1);
  });

  it("setEvents 与 SSE 实时事件经 event_id 去重合并", () => {
    // 历史回放含 a1，随后 SSE 又推来 a1（同一事件）
    useEventStore.getState().setEvents([makeEvent("a1", { task_id: "A" })], "A");
    useEventStore.getState().appendEvent(makeEvent("a1", { task_id: "A" }));
    const st = useEventStore.getState();
    expect(st.events).toHaveLength(1);
    expect(st.eventsByTaskId["A"]).toHaveLength(1);
  });
});

describe("eventStore — invalidateTask 缓存失效", () => {
  it("invalidateTask 删除指定任务的 events / eventsByTaskId / eventsByTurnId 与去重集合", () => {
    // 任务 A 含两个轮次，任务 B 一个轮次（各自按 task_id 批次灌入，符合 openTask 真实用法）
    useEventStore.getState().setEvents(
      [
        makeEvent("a1", { task_id: "A", turn_id: "t1" }),
        makeEvent("a2", { task_id: "A", turn_id: "t2" }),
      ],
      "A",
    );
    useEventStore.getState().setEvents([makeEvent("b1", { task_id: "B", turn_id: "t3" })], "B");

    useEventStore.getState().invalidateTask("A");

    const st = useEventStore.getState();
    // 任务 A 全量清除
    expect(st.eventsByTaskId["A"]).toBeUndefined();
    expect(st.eventsByTurnId["t1"]).toBeUndefined();
    expect(st.eventsByTurnId["t2"]).toBeUndefined();
    expect(st.events.filter((e) => e.task_id === "A")).toHaveLength(0);
    expect(st.processedEventIds.has("a1")).toBe(false);
    expect(st.processedEventIds.has("a2")).toBe(false);
    // 任务 B 不受影响
    expect(st.eventsByTaskId["B"]).toHaveLength(1);
    expect(st.eventsByTurnId["t3"]).toHaveLength(1);
    expect(st.processedEventIds.has("b1")).toBe(true);
  });

  it("invalidateTask 对无缓存的任务为空操作（不报错）", () => {
    expect(() => useEventStore.getState().invalidateTask("ghost")).not.toThrow();
    expect(useEventStore.getState().events).toHaveLength(0);
  });
});

describe("eventStore — appendEvents 批量提交", () => {
  it("批量追加并按 event_id 去重，单次提交不重复", () => {
    useEventStore.getState().clearEvents();
    useEventStore.getState().appendEvents([
      makeEvent("b1", { task_id: "A", turn_id: "t1" }),
      makeEvent("b2", { task_id: "A", turn_id: "t1" }),
      makeEvent("b1", { task_id: "A", turn_id: "t1" }), // 攒批内重复
    ]);
    const st = useEventStore.getState();
    expect(st.events).toHaveLength(2);
    expect(st.eventsByTurnId["t1"]).toHaveLength(2);
    expect(st.processedEventIds.has("b1")).toBe(true);
    expect(st.processedEventIds.has("b2")).toBe(true);
  });

  it("空批次为空操作（不报错、不新增）", () => {
    useEventStore.getState().clearEvents();
    useEventStore.getState().appendEvents([]);
    expect(useEventStore.getState().events).toHaveLength(0);
  });

  it("appendEvents 保持按 turn 分片的引用隔离（其它 turn 不受影响）", () => {
    useEventStore.getState().clearEvents();
    useEventStore.getState().appendEvents([makeEvent("p1", { task_id: "A", turn_id: "t1" })]);
    const before = useEventStore.getState().eventsByTurnId;
    // 新批次只动 t2，t1 的数组引用应保持不变（支撑 TurnTimeline 的 memo 跳过）
    useEventStore.getState().appendEvents([makeEvent("p2", { task_id: "A", turn_id: "t2" })]);
    const after = useEventStore.getState().eventsByTurnId;
    expect(after["t1"]).toBe(before["t1"]);
    expect(after["t2"]).toHaveLength(1);
  });
});
