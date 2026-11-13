import { afterEach, describe, expect, it, vi } from "vitest";
import { TransportFrameStore } from "@/lib/assistant/transport/transport-frame-store";
import { beginCommand, forgetCommands, reconcileCommands, stopTarget, useCommandRequests } from "@/lib/assistant/command-request-store";
import { useBackgroundTasksStore } from "@/lib/background-tasks/background-tasks-store";
import { activity, assistantMessage, toolCallPart, transportState } from "./transport-fixtures";

const entry = { toolCallId: "c0", kind: "terminal_command" as const, status: "running" as const, label: "任务", startedAt: "2026-11-07T00:00:00Z", endedAt: null };
afterEach(() => { forgetCommands([6]); useBackgroundTasksStore.getState().forget([6]); vi.unstubAllGlobals(); });

describe("shared loaded-window tool index", () => {
  it("streamed prose never reads unchanged history parts or republishes the background part table", () => {
    let historicalReads = 0;
    const messages = Array.from({ length: 1000 }, (_, i) => {
      const parts = [toolCallPart(`c${i}`, "execute_terminal", { status: "pending" })];
      return { ...assistantMessage(`m${i}`, 1, parts), get parts() { historicalReads++; return parts; } };
    });
    const store = new TransportFrameStore(transportState({ messages: [...messages, assistantMessage("live", 2, [{ type: "text", text: "a" }])], background_tasks: [entry], activity: activity(2, "running") }));
    const commit = () => {
      const snapshot = store.getSnapshot();
      reconcileCommands(6, snapshot.state.activity);
      useBackgroundTasksStore.getState().sync(6, { ...snapshot.state, toolCalls: snapshot.toolCalls });
    };
    commit();
    for (let i = 0; i < 20; i++) beginCommand(6, stopTarget(`c${i}`));
    const before = store.getSnapshot().toolCalls;
    const table = useBackgroundTasksStore.getState().parts[6];
    const listener = vi.fn();
    const off = useBackgroundTasksStore.subscribe(listener);
    historicalReads = 0;
    store.applyFrame({ task_id: 6, kind: "mutation", mutations: [{ kind: "append-text", path: ["messages", 1000, "parts", 0, "text"], value: "b" }] });
    store.flushScheduledPublication();
    commit();
    expect(historicalReads).toBe(0);
    expect(store.getSnapshot().toolCalls).toBe(before);
    expect(useBackgroundTasksStore.getState().parts[6]).toBe(table);
    expect(listener).not.toHaveBeenCalled();
    expect(Object.keys(useCommandRequests.getState().tasks[6])).toHaveLength(20);
    off();
  });

  it("updates changed tools without mutating published maps; full replacement removes old-window members", () => {
    const original = toolCallPart("c", "read_file");
    const store = new TransportFrameStore(transportState({ messages: [assistantMessage("m", 1, [original])] }));
    const before = store.getSnapshot().toolCalls;
    store.applyFrame({ task_id: 6, kind: "mutation", mutations: [{ kind: "set", path: ["messages", 0, "parts", 0, "status"], value: "completed" }] });
    expect(store.getSnapshot().toolCalls.get("c")?.status).toBe("completed");
    expect(before.get("c")).toBe(original);
    expect(before.get("c")?.status).toBe("running");
    store.applyFrame({ task_id: 6, kind: "full", mutations: [], state: transportState({ messages: [assistantMessage("new", 2, [toolCallPart("other", "read_file")])] }) });
    expect([...store.getSnapshot().toolCalls.keys()]).toEqual(["other"]);
  });

  it("indexes paged history after replaying buffered updates and discards obsolete fetches", () => {
    const recent = assistantMessage("recent", 2);
    const store = new TransportFrameStore(transportState({ messageOffset: 1, messages: [recent] }));
    const fetch = store.beginHistoryFetch()!;
    store.applyFrame({ task_id: 6, kind: "mutation", mutations: [{ kind: "set", path: ["messages", 0, "parts", 0, "status"], value: "cancelled" }] });
    expect(store.getSnapshot().toolCalls.size).toBe(0);
    expect(store.completeHistoryFetch(fetch, { messageOffset: 0, messages: [assistantMessage("old", 1, [toolCallPart("old-call", "read_file")])] })).toBe(true);
    expect(store.getSnapshot().toolCalls.get("old-call")?.status).toBe("cancelled");
    store.applyFrame({ task_id: 6, kind: "full", mutations: [], state: transportState({ messageOffset: 1, messages: [recent] }) });
    const stale = store.beginHistoryFetch()!;
    store.applyFrame({ task_id: 6, kind: "full", mutations: [], state: transportState() });
    expect(store.completeHistoryFetch(stale, { messageOffset: 0, messages: [assistantMessage("old", 1, [toolCallPart("old-call", "read_file")])] })).toBe(false);
    expect(store.getSnapshot().toolCalls.size).toBe(0);
  });
});
