import { describe, expect, it } from "vitest";

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

    const second = store.getSnapshot();
    const secondItems = second.items.filter((item) => item.kind === "canonical");
    expect(second.items).not.toBe(first.items);
    expect(secondItems[0]).toBe(firstItems[0]);
    expect(secondItems[1]).not.toBe(firstItems[1]);
    expect(second.state.runs[0].messages[1]).toMatchObject({
      parts: [{ type: "text", text: "partial update" }],
    });
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
});
