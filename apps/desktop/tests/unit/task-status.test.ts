import { afterEach, describe, expect, it } from "vitest";

import { useTaskStatusStore } from "@/lib/task-status/task-status-store";
import { parseStatusFrame } from "@/lib/task-status/use-task-status-stream";

describe("task status stream frames", () => {
  it("整表与变化帧把 Task id 键解析成数字，状态可为 null", () => {
    expect(parseStatusFrame({ type: "full", statuses: { "7": "running", "8": null } })).toEqual({
      type: "full", statuses: { 7: "running", 8: null },
    });
    expect(parseStatusFrame({ type: "heartbeat" })).toEqual({ type: "heartbeat" });
  });

  it("拒绝未知帧、非法 id 与非字符串状态", () => {
    expect(() => parseStatusFrame({ type: "other" })).toThrow();
    expect(() => parseStatusFrame({ type: "changed", statuses: { abc: "running" } })).toThrow();
    expect(() => parseStatusFrame({ type: "changed", statuses: { "1": 3 } })).toThrow();
  });
});

describe("task status store", () => {
  afterEach(() => useTaskStatusStore.setState({ statuses: {}, connected: false }));

  it("整表替换、变化合并；断开时保留上次的值", () => {
    const store = useTaskStatusStore.getState();
    store.replaceAll({ 1: "running", 2: "completed" });
    store.merge({ 1: "suspended" });
    expect(useTaskStatusStore.getState().statuses).toEqual({ 1: "suspended", 2: "completed" });
    store.replaceAll({ 3: null });
    expect(useTaskStatusStore.getState().statuses).toEqual({ 3: null });

    store.setConnected(true);
    store.setConnected(false);
    expect(useTaskStatusStore.getState().statuses).toEqual({ 3: null });
    expect(useTaskStatusStore.getState().connected).toBe(false);
  });
});
