import { describe, expect, it, vi } from "vitest";

import type { TransportMessage, TransportState } from "@/lib/assistant/contract";
import type { UserAddMessageCommand } from "@/lib/assistant/converter";
import {
  parseTransportFrame,
  TransportFrameStore,
  type ActivityTransition,
  type TransportFrameEnvelope,
} from "@/lib/assistant/transport-frame-store";
import { activity, assistantMessage, outcome, queuedMessage, transportState, userMessage } from "./transport-fixtures";

const streaming = (id: string, text: string): TransportMessage => (
  assistantMessage(id, 1, [{ type: "text", text, status: "running" }])
);

const running = (messages: TransportMessage[]): TransportState => transportState({
  messages,
  activity: activity(1, "running"),
});

const mutation = (mutations: TransportFrameEnvelope["mutations"]): TransportFrameEnvelope => ({
  task_id: 1, kind: "mutation", mutations,
});
const full = (state: TransportState): TransportFrameEnvelope => ({ task_id: 1, kind: "full", mutations: [], state });

const command = (text: string, commandId?: string): UserAddMessageCommand => ({
  type: "add-message",
  ...(commandId ? { commandId } : {}),
  message: { role: "user", parts: [{ type: "text", text }] },
});

const deferredScheduler = () => {
  const callbacks: Array<(frameTimestamp?: number) => void> = [];
  return {
    callbacks,
    scheduleCommit: (callback: (frameTimestamp?: number) => void) => {
      callbacks.push(callback);
      return { cancel: () => undefined };
    },
  };
};

const probe = () => ({
  startTiming: vi.fn(() => 1),
  finishTiming: vi.fn(() => 2),
  recordFrame: vi.fn(),
  recordPublication: vi.fn(),
  recordTerminalConvergence: vi.fn(),
  beginStream: vi.fn(),
  finishStream: vi.fn(),
  recordReactCommit: vi.fn(),
});

const recordTransitions = (store: TransportFrameStore) => {
  const transitions: ActivityTransition[] = [];
  store.subscribeActivity((transition) => transitions.push(transition));
  return transitions;
};

describe("TransportFrameStore", () => {
  it("rejects malformed wire frames before projection", () => {
    expect(() => parseTransportFrame({ kind: "mutation", mutations: [] })).toThrow("task_id");
    expect(() => parseTransportFrame({
      task_id: 1,
      kind: "mutation",
      mutations: [{ kind: "append-text", path: ["messages", { invalid: true }], value: "x" }],
    })).toThrow("path");
  });

  it("applies an append-text mutation with a new message array and stable untouched message", () => {
    const store = new TransportFrameStore(running([streaming("m-1", "old"), streaming("m-2", "partial")]));
    const first = store.getSnapshot();

    store.applyFrame(mutation([{ kind: "append-text", path: ["messages", 1, "parts", 0, "text"], value: " update" }]));
    store.flushScheduledPublication();

    const second = store.getSnapshot();
    expect(second.items).not.toBe(first.items);
    expect(second.items[0]).toBe(first.items[0]);
    expect(second.items[1]).not.toBe(first.items[1]);
    expect(second.state.messages[1]).toMatchObject({ parts: [{ type: "text", text: "partial update" }] });
  });

  it("batches streaming mutations into one animation-frame publication without touching history identities", () => {
    const scheduler = deferredScheduler();
    const historical = Array.from({ length: 1000 }, (_, index) => streaming(`history-${index}`, `old-${index}`));
    const store = new TransportFrameStore(running([...historical, streaming("active", "partial")]), scheduler);
    const before = store.getSnapshot().items.slice(0, historical.length);
    let publicationCount = 0;
    store.subscribe(() => { publicationCount += 1; });

    for (let index = 0; index < 1000; index += 1) {
      store.applyFrame(mutation([{ kind: "append-text", path: ["messages", 1000, "parts", 0, "text"], value: ` ${index}` }]));
    }

    expect(publicationCount).toBe(0);
    expect(scheduler.callbacks).toHaveLength(1);
    scheduler.callbacks.shift()?.(1);

    const after = store.getSnapshot();
    expect(publicationCount).toBe(1);
    expect(after.items.slice(0, historical.length).every((item, index) => item === before[index])).toBe(true);
  });

  it("does not defer content mutations that target a historical message", () => {
    const scheduler = deferredScheduler();
    const store = new TransportFrameStore(running([streaming("history", "old"), streaming("active", "partial")]), scheduler);
    let publicationCount = 0;
    store.subscribe(() => { publicationCount += 1; });

    store.applyFrame(mutation([{ kind: "append-text", path: ["messages", 0, "parts", 0, "text"], value: " changed" }]));

    expect(publicationCount).toBe(1);
    expect(scheduler.callbacks).toHaveLength(0);
  });

  it("reports frame and publication timings through the optional development probe", () => {
    const performanceProbe = probe();
    const scheduler = deferredScheduler();
    const store = new TransportFrameStore(running([streaming("active", "partial")]), { performanceProbe, ...scheduler });

    store.applyFrame(mutation([{ kind: "append-text", path: ["messages", 0, "parts", 0, "text"], value: " update" }]));
    scheduler.callbacks.shift()?.(1);

    expect(performanceProbe.recordFrame).toHaveBeenCalledWith(expect.objectContaining({ kind: "mutation", mutationCount: 1, durationMs: 2 }));
    expect(performanceProbe.recordPublication).toHaveBeenCalledWith(expect.objectContaining({
      itemCount: 1, activeMessageId: "active", publicationKind: "stream", animationFrameTimestamp: 1,
    }));
  });

  it("marks a slow connection for resync and ignores later mutations until a full frame", () => {
    const store = new TransportFrameStore(running([streaming("m-1", "old")]));
    const before = store.getSnapshot();
    store.applyFrame({ task_id: 1, kind: "resync_required", mutations: [], resync_reason: "subscriber_backpressure" });
    expect(store.getSnapshot().resyncRequired).toBe(true);
    expect(store.getSnapshot().state).toBe(before.state);

    store.applyFrame(mutation([{ kind: "append-text", path: ["messages", 0, "parts", 0, "text"], value: " stale" }]));
    expect(store.getSnapshot().state).toBe(before.state);

    store.applyFrame(full(running([streaming("m-1", "fresh")])));
    expect(store.getSnapshot().resyncRequired).toBe(false);
  });

  it("活动元数据更新不重建历史消息，活动状态变化刷新当前 Run 的消息上下文", () => {
    const store = new TransportFrameStore(running([userMessage("1", 1), streaming("assistant-1", "x")]));
    const before = store.getSnapshot();

    store.applyFrame(mutation([{ kind: "set", path: ["activity", "contextWindowTotal"], value: 120_000 }]));
    const metadata = store.getSnapshot();
    expect(metadata.state.activity?.contextWindowTotal).toBe(120_000);
    expect(metadata.items[0]).toBe(before.items[0]);

    store.applyFrame(mutation([
      { kind: "set", path: ["activity", "status"], value: "completed" },
      { kind: "set", path: ["messages", 1, "outcome"], value: outcome("completed") },
    ]));
    const settled = store.getSnapshot();
    expect(settled.items[1]).not.toBe(metadata.items[1]);
    expect(settled.items[1]).toMatchObject({ kind: "canonical", context: { isRunEnd: true, outcome: { status: "completed" } } });
  });

  it("每个 Run 只有最后一条助手消息带回复末尾上下文，结局取自消息自身", () => {
    const store = new TransportFrameStore(transportState({
      messages: [
        userMessage("1", 1),
        assistantMessage("assistant-1", 1, undefined, outcome("cancelled")),
        userMessage("2", 2),
        assistantMessage("assistant-run-2", 2),
        assistantMessage("assistant-2", 2),
      ],
      activity: activity(2, "running"),
    }));
    const contexts = store.getSnapshot().items.map((item) => item.kind === "canonical" ? item.context : null);
    expect(contexts[1]).toMatchObject({ runId: 1, isRunEnd: true, outcome: { status: "cancelled" }, activity: null });
    expect(contexts[3]).toMatchObject({ runId: 2, isRunEnd: false });
    expect(contexts[4]).toMatchObject({ runId: 2, isRunEnd: true, outcome: null, activity: { runId: 2, status: "running" } });
  });

  it("moves a queued message into the conversation without duplicating its stable identity", () => {
    const store = new TransportFrameStore({ ...running([streaming("a1", "working")]), queued_messages: [queuedMessage("q1", "later", "immediate")] });
    const ids = () => store.getSnapshot().items.map((item) => item.kind === "canonical" ? item.message.id : item.kind);
    expect(ids()).toEqual(["a1", "q1"]);
    store.applyFrame(mutation([
      { kind: "set", path: ["queued_messages"], value: [] },
      { kind: "set", path: ["messages", 1], value: userMessage("q1", 1, "later") },
    ]));
    expect(ids()).toEqual(["a1", "q1"]);
  });

});

describe("TransportFrameStore 一轮结束（D2）", () => {
  it("首次构造只记基线，不触发 settled", () => {
    const store = new TransportFrameStore(transportState({ activity: activity(1, "completed") }));
    const transitions = recordTransitions(store);
    store.applyFrame(mutation([{ kind: "set", path: ["activity", "usage"], value: null }]));
    expect(transitions).toEqual([]);
  });

  it("活动态进入终态时只触发一次 settled，后续无关提交不再触发", () => {
    const performanceProbe = probe();
    const store = new TransportFrameStore(running([streaming("assistant-run-1", "x")]), { performanceProbe });
    const transitions = recordTransitions(store);

    store.applyFrame(mutation([{ kind: "set", path: ["activity", "status"], value: "cancelled" }]));
    store.setPendingCommands([command("cleanup")]);
    store.clearPendingCommands();
    store.applyFrame(mutation([{ kind: "set", path: ["activity", "endReason"], value: "cancelled" }]));

    expect(transitions).toEqual([{ kind: "settled", status: "cancelled" }]);
    expect(performanceProbe.recordTerminalConvergence).toHaveBeenCalledTimes(1);
  });

  it("挂起仍是活动态，不触发 settled；挂起后被取消才触发", () => {
    const store = new TransportFrameStore(running([]));
    const transitions = recordTransitions(store);
    store.applyFrame(mutation([{ kind: "set", path: ["activity", "status"], value: "suspended" }]));
    expect(transitions).toEqual([]);
    store.applyFrame(mutation([{ kind: "set", path: ["activity", "status"], value: "cancelled" }]));
    expect(transitions).toEqual([{ kind: "settled", status: "cancelled" }]);
  });

  it("重连拿到 full 帧时补触发一次 settled", () => {
    const store = new TransportFrameStore(running([]));
    const transitions = recordTransitions(store);
    store.applyFrame(full(transportState({ activity: activity(1, "failed") })));
    store.applyFrame(full(transportState({ activity: activity(1, "failed") })));
    expect(transitions).toEqual([{ kind: "settled", status: "failed" }]);
  });

  it("importState 与 full 帧同一语义", () => {
    const store = new TransportFrameStore(running([]));
    const transitions = recordTransitions(store);
    store.importState(transportState({ activity: activity(1, "completed") }));
    expect(transitions).toEqual([{ kind: "settled", status: "completed" }]);
  });

  it("full 帧里旧 Run 已结束、新 Run 已开始：按 runId 变化补 settled 再 started", () => {
    const store = new TransportFrameStore(running([]));
    const transitions = recordTransitions(store);
    store.applyFrame(full(transportState({
      messages: [assistantMessage("assistant-run-1", 1, [], outcome("completed")), userMessage("2", 2)],
      activity: activity(2, "running"),
    })));
    expect(transitions).toEqual([{ kind: "settled", status: "completed" }, { kind: "started", status: "running" }]);
  });

  it("从无活动进入活动态触发 started", () => {
    const store = new TransportFrameStore(transportState());
    const transitions = recordTransitions(store);
    store.applyFrame(mutation([{ kind: "set", path: ["activity"], value: activity(1, "running") }]));
    expect(transitions).toEqual([{ kind: "started", status: "running" }]);
  });
});

describe("TransportFrameStore 运行中判定（D3）", () => {
  it.each([
    ["running", true],
    ["suspended", true],
    ["completed", false],
    ["failed", false],
    ["cancelled", false],
  ] as const)("活动状态 %s → isRunning=%s", (status, expected) => {
    expect(new TransportFrameStore(transportState({ activity: activity(1, status) })).getSnapshot().isRunning).toBe(expected);
  });

  it("没有活动但有待提交命令时也是运行中", () => {
    const store = new TransportFrameStore(transportState());
    expect(store.getSnapshot().isRunning).toBe(false);
    store.setPendingCommands([command("hi")]);
    expect(store.getSnapshot().isRunning).toBe(true);
  });
});

describe("TransportFrameStore 乐观命令收敛（D8）", () => {
  it("snapshot 出现新的用户消息才清除对应的乐观命令，活动与内容变化不清", () => {
    const store = new TransportFrameStore(transportState({ messages: [userMessage("1", 1)], activity: activity(1, "completed") }));
    store.setPendingCommands([command("first", "c1"), command("second", "c2")]);

    store.applyFrame(mutation([{ kind: "set", path: ["activity"], value: activity(2, "running") }]));
    expect(store.getSnapshot().pendingCommands).toHaveLength(2);

    store.applyFrame(mutation([{ kind: "set", path: ["messages", 1], value: userMessage("2", 2, "first") }]));
    expect(store.getSnapshot().pendingCommands.map((item) => item.commandId)).toEqual(["c2"]);

    store.applyFrame(mutation([{ kind: "set", path: ["queued_messages"], value: [queuedMessage("3", "second")] }]));
    expect(store.getSnapshot().pendingCommands).toEqual([]);
  });

  it("clears only the rejected command pending projection", () => {
    const store = new TransportFrameStore(transportState());
    const first = command("first", "command-1");
    const second = command("second", "command-2");
    store.setPendingCommands([first, second]);
    store.clearPendingCommands([first]);
    expect(store.getSnapshot().pendingCommands).toEqual([second]);
  });
});
