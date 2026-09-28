import { describe, expect, it, vi } from "vitest";

import type { TransportMessage, TransportState } from "@/lib/assistant/contract";
import { parseTransportFrame, TransportFrameStore } from "@/lib/assistant/transport-frame-store";

const baseMessage = (id: string, text: string): TransportMessage => ({
  id,
  role: "assistant",
  parts: [{ type: "text", text, status: "running" }],
});

const state = (messages: TransportMessage[]): TransportState => ({
  runs: [{
    runId: 1,
    status: "running",
    endReason: null,
    messages,
    usage: null,
    error: null,
  }],
  current_run_id: 1,
  approvals: {},
  context_usage_ratio: null,
  context_usage_used: null,
  context_window_total: null,
  error: null,
});

describe("TransportFrameStore", () => {
  it("rejects malformed wire frames before projection", () => {
    expect(() => parseTransportFrame({ kind: "mutation", mutations: [] })).toThrow("task_id");
    expect(() => parseTransportFrame({
      task_id: 1,
      kind: "mutation",
      mutations: [{ kind: "append-text", path: ["runs", { invalid: true }], value: "x" }],
      target_run_id: 1,
    })).toThrow("path");
  });

  it("applies an append-text mutation with a new message array and stable untouched message", () => {
    const historical = baseMessage("m-1", "old");
    const active = baseMessage("m-2", "partial");
    const store = new TransportFrameStore(state([historical, active]));
    const first = store.getSnapshot();
    const firstItems = first.items.filter((item) => item.kind === "canonical");

    store.applyFrame({
      task_id: 1,
      kind: "mutation",
      mutations: [{
        kind: "append-text",
        path: ["runs", 0, "messages", 1, "parts", 0, "text"],
        value: " update",
      }],
      source_run_id: 1,
      target_run_id: 1,
      target_run_status: "running",
    });
    store.flushScheduledPublication();

    const second = store.getSnapshot();
    const secondItems = second.items.filter((item) => item.kind === "canonical");
    expect(second.items).not.toBe(first.items);
    expect(secondItems[0]).toBe(firstItems[0]);
    expect(secondItems[1]).not.toBe(firstItems[1]);
    expect(second.state.runs[0].messages[1]).toMatchObject({
      parts: [{ type: "text", text: "partial update" }],
    });
  });

  it("batches streaming mutations into one animation-frame publication without touching history identities", () => {
    const callbacks: Array<(frameTimestamp?: number) => void> = [];
    const historical = Array.from({ length: 1000 }, (_, index) => baseMessage(`history-${index}`, `old-${index}`));
    const active = baseMessage("active", "partial");
    const store = new TransportFrameStore(state([...historical, active]), {
      scheduleCommit: (callback) => {
        callbacks.push(callback);
        return { cancel: () => undefined };
      },
    });
    const before = store.getSnapshot().items.slice(0, historical.length);
    let publicationCount = 0;
    store.subscribe(() => {
      publicationCount += 1;
    });

    for (let index = 0; index < 1000; index += 1) {
      store.applyFrame({
        task_id: 1,
        kind: "mutation",
        mutations: [{
          kind: "append-text",
          path: ["runs", 0, "messages", 1000, "parts", 0, "text"],
          value: ` ${index}`,
        }],
        target_run_id: 1,
        target_run_status: "running",
      });
    }

    expect(publicationCount).toBe(0);
    expect(callbacks).toHaveLength(1);
    callbacks.shift()?.(1);

    const after = store.getSnapshot();
    expect(publicationCount).toBe(1);
    expect(after.items.slice(0, historical.length)).toEqual(before);
    expect(after.items.slice(0, historical.length).every((item, index) => item === before[index])).toBe(true);
    expect(after.items.at(-1)).not.toBe(before.at(-1));
  });

  it("publishes at most once per animation frame and converges a completed run once", () => {
    const callbacks: Array<(frameTimestamp?: number) => void> = [];
    const store = new TransportFrameStore(state([baseMessage("active", "partial")]), {
      scheduleCommit: (callback) => {
        callbacks.push(callback);
        return { cancel: () => undefined };
      },
    });
    let publicationCount = 0;
    store.subscribe(() => {
      publicationCount += 1;
    });

    for (const suffix of [" one", " two", " three"]) {
      store.applyFrame({
        task_id: 1,
        kind: "mutation",
        mutations: [{
          kind: "append-text",
          path: ["runs", 0, "messages", 0, "parts", 0, "text"],
          value: suffix,
        }],
        target_run_id: 1,
        target_run_status: "running",
      });
    }
    expect(publicationCount).toBe(0);
    callbacks.shift()?.();
    expect(publicationCount).toBe(1);

    store.applyFrame({
      task_id: 1,
      kind: "mutation",
      mutations: [{ kind: "set", path: ["runs", 0, "status"], value: "completed" }],
      target_run_id: 1,
      target_run_status: "completed",
    });

    expect(publicationCount).toBe(2);
    expect(store.getSnapshot().state.runs[0]?.status).toBe("completed");
    expect(callbacks).toHaveLength(0);
  });

  it("does not defer content mutations that target a historical message", () => {
    const callbacks: Array<(frameTimestamp?: number) => void> = [];
    const store = new TransportFrameStore(state([
      baseMessage("history", "old"),
      baseMessage("active", "partial"),
    ]), {
      scheduleCommit: (callback) => {
        callbacks.push(callback);
        return { cancel: () => undefined };
      },
    });
    let publicationCount = 0;
    store.subscribe(() => {
      publicationCount += 1;
    });

    store.applyFrame({
      task_id: 1,
      kind: "mutation",
      mutations: [{
        kind: "append-text",
        path: ["runs", 0, "messages", 0, "parts", 0, "text"],
        value: " changed",
      }],
      target_run_id: 1,
      target_run_status: "running",
    });

    expect(publicationCount).toBe(1);
    expect(callbacks).toHaveLength(0);
  });

  it("reports frame and publication timings through the optional development probe", () => {
    const performanceProbe = {
      startTiming: vi.fn(() => 1),
      finishTiming: vi.fn(() => 2),
      recordFrame: vi.fn(),
      recordPublication: vi.fn(),
      recordTerminalConvergence: vi.fn(),
      beginStream: vi.fn(),
      finishStream: vi.fn(),
      recordReactCommit: vi.fn(),
    };
    const callbacks: Array<(frameTimestamp?: number) => void> = [];
    const store = new TransportFrameStore(state([baseMessage("active", "partial")]), {
      performanceProbe,
      scheduleCommit: (callback) => {
        callbacks.push(callback);
        return { cancel: () => undefined };
      },
    });

    store.applyFrame({
      task_id: 1,
      kind: "mutation",
      mutations: [{
        kind: "append-text",
        path: ["runs", 0, "messages", 0, "parts", 0, "text"],
        value: " update",
      }],
      target_run_id: 1,
      target_run_status: "running",
    });
    callbacks.shift()?.(1);

    expect(performanceProbe.recordFrame).toHaveBeenCalledWith(expect.objectContaining({
      kind: "mutation",
      mutationCount: 1,
      durationMs: 2,
    }));
    expect(performanceProbe.recordPublication).toHaveBeenCalledWith(expect.objectContaining({
      durationMs: 2,
      itemCount: 1,
      activeMessageId: "active",
      publicationKind: "stream",
      animationFrameTimestamp: 1,
      terminalConvergence: false,
    }));
  });

  it("marks a slow connection for resync without changing canonical messages", () => {
    const message = baseMessage("m-1", "old");
    const store = new TransportFrameStore(state([message]));
    const before = store.getSnapshot();
    store.applyFrame({
      task_id: 1,
      kind: "resync_required",
      mutations: [],
      target_run_id: 1,
      target_run_status: "running",
      resync_reason: "subscriber_backpressure",
    });
    const after = store.getSnapshot();
    expect(after.resyncRequired).toBe(true);
    expect(after.state).toBe(before.state);
    expect(after.items).toBe(before.items);

    store.applyFrame({
      task_id: 1,
      kind: "mutation",
      mutations: [{
        kind: "append-text",
        path: ["runs", 0, "messages", 0, "parts", 0, "text"],
        value: " stale",
      }],
      target_run_id: 1,
      target_run_status: "running",
    });
    expect(store.getSnapshot().state).toBe(before.state);
  });

  it("publishes a terminal convergence only once when later cleanup publishes occur", () => {
    const performanceProbe = {
      startTiming: vi.fn(() => 1),
      finishTiming: vi.fn(() => 2),
      recordFrame: vi.fn(),
      recordPublication: vi.fn(),
      recordTerminalConvergence: vi.fn(),
      beginStream: vi.fn(),
      finishStream: vi.fn(),
      recordReactCommit: vi.fn(),
    };
    const store = new TransportFrameStore(state([baseMessage("active", "partial")]), { performanceProbe });

    store.applyFrame({
      task_id: 1,
      kind: "mutation",
      mutations: [{ kind: "set", path: ["runs", 0, "status"], value: "completed" }],
      target_run_id: 1,
      target_run_status: "completed",
    });
    store.setPendingCommands([{
      type: "add-message",
      message: { role: "user", parts: [{ type: "text", text: "cleanup" }] },
    }]);
    store.clearPendingCommands();

    expect(performanceProbe.recordTerminalConvergence).toHaveBeenCalledTimes(1);
  });

  it("keeps message entries stable for task metadata and only rebuilds the affected run", () => {
    const historical = baseMessage("m-1", "old");
    const store = new TransportFrameStore(state([historical]));
    const before = store.getSnapshot();
    const beforeItem = before.items[0];

    store.applyFrame({
      task_id: 1,
      kind: "mutation",
      mutations: [{ kind: "set", path: ["context_usage_used"], value: 12 }],
      target_run_id: 1,
      target_run_status: "running",
    });
    const metadataChange = store.getSnapshot();
    expect(metadataChange.items).toBe(before.items);
    expect(metadataChange.state).not.toBe(before.state);

    store.applyFrame({
      task_id: 1,
      kind: "mutation",
      mutations: [{ kind: "set", path: ["runs", 0, "status"], value: "completed" }],
      target_run_id: 1,
      target_run_status: "completed",
    });
    const statusChange = store.getSnapshot();
    expect(statusChange.items).not.toBe(metadataChange.items);
    expect(statusChange.items[0]).not.toBe(beforeItem);
  });

  it("rebuilds the canonical projection when a new run is inserted", () => {
    const store = new TransportFrameStore(state([]));
    store.applyFrame({
      task_id: 1,
      kind: "mutation",
      mutations: [{
        kind: "set",
        path: ["runs", 0],
        value: {
          runId: 2,
          status: "pending",
          endReason: null,
          messages: [baseMessage("user-2", ""), baseMessage("assistant-2", "")],
          usage: null,
          error: null,
        },
      }],
      target_run_id: 2,
      target_run_status: "pending",
    });

    expect(store.getSnapshot().items.filter((item) => item.kind === "canonical")).toHaveLength(2);
    expect(store.getSnapshot().targetRunId).toBe(2);
  });

  it("full frames replace the canonical projection and clear optimistic commands", () => {
    const store = new TransportFrameStore(state([]));
    const command = {
      type: "add-message" as const,
      message: { role: "user" as const, parts: [{ type: "text" as const, text: "hello" }] },
    };
    store.setPendingCommands([command]);
    expect(store.getSnapshot().pendingCommands).toHaveLength(1);
    store.applyFrame({
      task_id: 1,
      kind: "full",
      mutations: [],
      state: state([{ ...baseMessage("m-1", "hello"), role: "user" }]),
      target_run_id: 1,
      target_run_status: "running",
    });
    expect(store.getSnapshot().pendingCommands).toHaveLength(0);
    expect(store.getSnapshot().items.filter((item) => item.kind === "canonical")).toHaveLength(1);
  });

  it("clears only the rejected command pending projection", () => {
    const store = new TransportFrameStore(state([]));
    const first = {
      type: "add-message" as const,
      commandId: "command-1",
      message: { role: "user" as const, parts: [{ type: "text" as const, text: "first" }] },
    };
    const second = {
      type: "add-message" as const,
      commandId: "command-2",
      message: { role: "user" as const, parts: [{ type: "text" as const, text: "second" }] },
    };

    store.setPendingCommands([first, second]);
    store.clearPendingCommands([first]);

    expect(store.getSnapshot().pendingCommands).toEqual([second]);
  });
});
