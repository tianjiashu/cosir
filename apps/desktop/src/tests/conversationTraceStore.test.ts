import { beforeEach, describe, expect, it } from "vitest";
import { getConversationTraceForTask, useConversationTraceStore } from "@/stores/conversationTraceStore";

beforeEach(() => {
  useConversationTraceStore.getState().resetConversationTraces();
});

describe("conversationTraceStore", () => {
  it("records latest trace globally and by task", () => {
    useConversationTraceStore.getState().recordTrace({
      traceId: "1234567890abcdef1234567890abcdef",
      taskId: "task-1",
      approvalId: "",
      operation: "task_stream",
      method: "GET",
      path: "/tasks/task-1/stream",
    });

    const state = useConversationTraceStore.getState();
    expect(state.traces).toHaveLength(1);
    expect(state.latestTrace?.traceId).toBe("1234567890abcdef1234567890abcdef");
    expect(getConversationTraceForTask("task-1")?.operation).toBe("task_stream");
    expect(state.streamTraceByTaskId["task-1"]?.traceId).toBe("1234567890abcdef1234567890abcdef");
    expect(state.latestTraceByTaskIdAndOperation["task-1"]?.task_stream?.traceId).toBe(
      "1234567890abcdef1234567890abcdef",
    );
  });

  it("keeps stream trace when later task requests update latest trace", () => {
    const store = useConversationTraceStore.getState();
    store.recordTrace({
      traceId: "11111111111111111111111111111111",
      taskId: "task-1",
      approvalId: "",
      operation: "task_stream",
      method: "GET",
      path: "/tasks/task-1/stream",
    });
    store.recordTrace({
      traceId: "22222222222222222222222222222222",
      taskId: "task-1",
      approvalId: "",
      operation: "task_events",
      method: "GET",
      path: "/tasks/task-1/events",
    });

    const state = useConversationTraceStore.getState();
    expect(state.latestTraceByTaskId["task-1"].traceId).toBe("22222222222222222222222222222222");
    expect(state.streamTraceByTaskId["task-1"].traceId).toBe("11111111111111111111111111111111");
  });

});
