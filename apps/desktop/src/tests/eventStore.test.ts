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

  it("selectLatestEvent 返回最后一条", () => {
    useEventStore.getState().setEvents([makeEvent("a"), makeEvent("b")]);
    expect(selectLatestEvent(useEventStore.getState())?.event_id).toBe("b");
  });

  it("selectEventCount 返回数量", () => {
    useEventStore.getState().setEvents([makeEvent("a"), makeEvent("b"), makeEvent("c")]);
    expect(selectEventCount(useEventStore.getState())).toBe(3);
  });

  it("空集合时 selectLatestEvent 返回 undefined", () => {
    expect(selectLatestEvent(useEventStore.getState())).toBeUndefined();
  });
});
