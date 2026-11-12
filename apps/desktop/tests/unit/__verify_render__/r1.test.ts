import { describe, expect, it } from "vitest";
import { ExternalStoreThreadRuntimeCore } from "@assistant-ui/core/internal";
// @ts-expect-error deep import
import { getActiveTopAnchorTurn } from "../../../node_modules/@assistant-ui/react/dist/primitives/thread/topAnchor/topAnchorTurn.js";
import { TransportFrameStore, convertFrameStoreItem } from "@/lib/assistant/transport/transport-frame-store";
import { activity, assistantMessage, queuedMessage, transportState, userMessage } from "../transport-fixtures";

const adapterOf = (store: TransportFrameStore) => {
  const s = store.getSnapshot();
  return { messages: s.items, isRunning: s.isRunning, state: s.state, convertMessage: convertFrameStoreItem, onNew: async () => {} } as never;
};

function run(withQueue: boolean) {
  const state = transportState({
    messages: [userMessage("u1", 1), assistantMessage("a1", 1, [{ type: "text", text: "x", status: "running" } as never])],
    activity: activity(1, "running"),
    ...(withQueue ? { queued_messages: [queuedMessage("q1")] } : {}),
  });
  const store = new TransportFrameStore(state);
  const core = new ExternalStoreThreadRuntimeCore({ getModelContext: () => ({}) } as never, adapterOf(store));
  const lastIds: string[] = []; const anchors: unknown[] = []; const a1IsLast: boolean[] = [];
  for (let i = 0; i < 4; i++) {
    store.applyFrame({ task_id: 1, kind: "mutation", mutations: [{ kind: "append-text", path: ["messages", 1, "parts", 0, "text"], value: "y" }] } as never);
    store.flushScheduledPublication();
    core.__internal_setAdapter(adapterOf(store));
    const msgs = core.messages;
    lastIds.push(msgs.at(-1)!.id);
    anchors.push(getActiveTopAnchorTurn({ isRunning: core.isRunning, messages: msgs }));
    a1IsLast.push(msgs.at(-1)!.id === "a1");
  }
  return { lastIds, anchors, a1IsLast, roles: core.messages.map((m) => `${m.id}:${m.role}:${(m.metadata as { isOptimistic?: boolean }).isOptimistic ?? ""}`) };
}

describe("R1", () => {
  it("有排队时每帧换 optimistic id", () => {
    const q = run(true);
    console.log("WITH QUEUE", JSON.stringify(q));
    expect(new Set(q.lastIds).size).toBe(4);
    const n = run(false);
    console.log("NO QUEUE", JSON.stringify(n));
    expect(new Set(n.lastIds).size).toBe(1);
  });
});
