import { afterEach, describe, expect, it } from "vitest";

import { useTaskStatusStore } from "@/lib/task-status/task-status-store";
import { parseStatusFrame } from "@/lib/task-status/use-task-status-stream";

describe("task status stream frames", () => {
  it("整表与变化帧把 Task id 键解析成数字，执行状态可为 null，等你操作是调用数", () => {
    expect(parseStatusFrame({
      type: "full",
      tasks: {
        "7": { executionStatus: "running", awaitingUserCalls: 0 },
        "8": { executionStatus: null, awaitingUserCalls: 2 },
      },
    })).toEqual({
      type: "full",
      tasks: { 7: { executionStatus: "running", awaitingUserCalls: 0 }, 8: { executionStatus: null, awaitingUserCalls: 2 } },
    });
    expect(parseStatusFrame({ type: "heartbeat" })).toEqual({ type: "heartbeat" });
  });

  it("拒绝未知帧、旧帧形状、非法 id、非字符串状态与非法调用数", () => {
    const entry = { executionStatus: "running", awaitingUserCalls: 0 };
    expect(() => parseStatusFrame({ type: "other" })).toThrow();
    expect(() => parseStatusFrame({ type: "changed", statuses: { "1": "running" } })).toThrow();
    expect(() => parseStatusFrame({ type: "changed", tasks: { abc: entry } })).toThrow();
    expect(() => parseStatusFrame({ type: "changed", tasks: { "1": { ...entry, executionStatus: 3 } } })).toThrow();
    expect(() => parseStatusFrame({ type: "changed", tasks: { "1": { ...entry, awaitingUserCalls: -1 } } })).toThrow();
    expect(() => parseStatusFrame({ type: "changed", tasks: { "1": { executionStatus: "running" } } })).toThrow();
  });
});

describe("task status store", () => {
  afterEach(() => useTaskStatusStore.setState({ tasks: {}, connected: false }));

  it("整表替换、变化合并；断开时保留上次的值", () => {
    const store = useTaskStatusStore.getState();
    const status = (executionStatus: string | null, awaitingUserCalls = 0) => ({ executionStatus, awaitingUserCalls });
    store.replaceAll({ 1: status("running"), 2: status("completed") });
    store.merge({ 1: status("completed", 1) });
    expect(useTaskStatusStore.getState().tasks).toEqual({ 1: status("completed", 1), 2: status("completed") });
    store.replaceAll({ 3: status(null) });
    expect(useTaskStatusStore.getState().tasks).toEqual({ 3: status(null) });

    store.setConnected(true);
    store.setConnected(false);
    expect(useTaskStatusStore.getState().tasks).toEqual({ 3: status(null) });
    expect(useTaskStatusStore.getState().connected).toBe(false);
  });
});
