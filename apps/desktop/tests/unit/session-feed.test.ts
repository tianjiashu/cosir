import { afterEach, describe, expect, it } from "vitest";

import { useSessionStatusStore } from "@/lib/session-status/session-status-store";
import { parseSessionFeedFrame } from "@/lib/session-feed/use-session-feed";

describe("session feed frames", () => {
  it("整表与变化帧把 Session id 键解析成数字，执行状态可为 null，等你操作是调用数", () => {
    expect(parseSessionFeedFrame({
      type: "full",
      sessions: {
        "7": { executionStatus: "running", awaitingUserCalls: 0 },
        "8": { executionStatus: null, awaitingUserCalls: 2 },
      },
    })).toEqual({
      type: "full",
      sessions: { 7: { executionStatus: "running", awaitingUserCalls: 0 }, 8: { executionStatus: null, awaitingUserCalls: 2 } },
    });
    expect(parseSessionFeedFrame({ type: "heartbeat" })).toEqual({ type: "heartbeat" });
  });

  it("拒绝未知帧、旧帧形状、非法 id、非字符串状态与非法调用数", () => {
    const entry = { executionStatus: "running", awaitingUserCalls: 0 };
    expect(() => parseSessionFeedFrame({ type: "other" })).toThrow();
    expect(() => parseSessionFeedFrame({ type: "changed", statuses: { "1": "running" } })).toThrow();
    expect(() => parseSessionFeedFrame({ type: "changed", sessions: { abc: entry } })).toThrow();
    expect(() => parseSessionFeedFrame({ type: "changed", sessions: { "1": { ...entry, executionStatus: 3 } } })).toThrow();
    expect(() => parseSessionFeedFrame({ type: "changed", sessions: { "1": { ...entry, awaitingUserCalls: -1 } } })).toThrow();
    expect(() => parseSessionFeedFrame({ type: "changed", sessions: { "1": { executionStatus: "running" } } })).toThrow();
  });

  it("目录失效帧带工作区列表开关与工作区 id", () => {
    expect(parseSessionFeedFrame({ type: "catalog", workspaces: false, workspaceIds: [3, 4] }))
      .toEqual({ type: "catalog", workspaces: false, workspaceIds: [3, 4] });
    expect(parseSessionFeedFrame({ type: "catalog", workspaces: true, workspaceIds: [] }))
      .toEqual({ type: "catalog", workspaces: true, workspaceIds: [] });
    expect(() => parseSessionFeedFrame({ type: "catalog", workspaceIds: [3] })).toThrow();
    expect(() => parseSessionFeedFrame({ type: "catalog", workspaces: false, workspaceIds: ["3"] })).toThrow();
    expect(() => parseSessionFeedFrame({ type: "catalog", workspaces: false, workspaceIds: [0] })).toThrow();
  });
});

describe("session status store", () => {
  afterEach(() => useSessionStatusStore.setState({ sessions: {}, connected: false }));

  it("整表替换、变化合并；断开时保留上次的值", () => {
    const store = useSessionStatusStore.getState();
    const status = (executionStatus: string | null, awaitingUserCalls = 0) => ({ executionStatus, awaitingUserCalls });
    store.replaceAll({ 1: status("running"), 2: status("completed") });
    store.merge({ 1: status("completed", 1) });
    expect(useSessionStatusStore.getState().sessions).toEqual({ 1: status("completed", 1), 2: status("completed") });
    store.replaceAll({ 3: status(null) });
    expect(useSessionStatusStore.getState().sessions).toEqual({ 3: status(null) });

    store.setConnected(true);
    store.setConnected(false);
    expect(useSessionStatusStore.getState().sessions).toEqual({ 3: status(null) });
    expect(useSessionStatusStore.getState().connected).toBe(false);
  });
});
