import { afterEach, describe, expect, it, vi } from "vitest";

import type { TransportMessage, TransportState } from "@/lib/assistant/contract";
import type { UserAddMessageCommand } from "@/lib/assistant/converter";
import {
  parseTransportFrame,
  TransportFrameStore,
  convertFrameStoreItem,
  type ActivityTransition,
  type TransportFrameEnvelope,
} from "@/lib/assistant/transport-frame-store";
import { activity, agentMessage, assistantMessage, outcome, toolCallPart, queuedMessage, transportState, userMessage } from "./transport-fixtures";

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

describe("增量与完整快照遵守同一当前契约", () => {
  const validTool = () => toolCallPart("c", "read_file");
  const stateWithTool = () => transportState({ messages: [assistantMessage("a", 1, [validTool()])] });

  it.each(["future-status", undefined])("完整快照拒绝非法工具状态 %s", (status) => {
    const state = stateWithTool();
    Object.assign(state.messages[0].parts[0], { status });
    expect(() => parseTransportFrame(full(state))).toThrow("status");
  });

  it.each(["status", "part", "parts", "message"])("%s 增量中的未知状态报错且整帧不提交", (level) => {
    const initial = stateWithTool();
    const store = new TransportFrameStore(initial);
    const listener = vi.fn();
    store.subscribe(listener);
    const invalid = { ...validTool(), status: "future-status" };
    const change = level === "status" ? { path: ["messages", 0, "parts", 0, "status"], value: "future-status" }
      : level === "part" ? { path: ["messages", 0, "parts", 0], value: invalid }
        : level === "parts" ? { path: ["messages", 0, "parts"], value: [invalid] }
          : { path: ["messages", 0], value: { ...initial.messages[0], parts: [invalid] } };
    expect(() => store.applyFrame(mutation([
      { kind: "set", path: ["messages", 0, "parts", 0, "intent"], value: "不得提交" },
      { kind: "set", ...change },
    ]))).toThrow("status");
    expect(store.getSnapshot().state).toBe(initial);
    expect(listener).not.toHaveBeenCalled();
    store.applyFrame(mutation([{ kind: "set", path: ["messages", 0, "parts", 0, "status"], value: "completed" }]));
    expect(store.getSnapshot().state.messages[0].parts[0]).toEqual({ ...validTool(), status: "completed" });
  });

  it("拒绝废弃审批字段和空终态耗时", () => {
    const store = new TransportFrameStore(stateWithTool());
    expect(() => store.applyFrame(mutation([{ kind: "set", path: ["messages", 0, "parts", 0, "approvalRequestId"], value: null }]))).toThrow("已知字段");
    expect(() => store.applyFrame(mutation([{ kind: "set", path: ["messages", 0, "outcome"], value: { ...outcome("completed"), durationMs: null } }]))).toThrow("durationMs");
  });

  it("历史页回放错误增量时保留原窗口", () => {
    const initial = transportState({ messageOffset: 1, messages: [assistantMessage("later", 1)] });
    const store = new TransportFrameStore(initial);
    const fetch = store.beginHistoryFetch()!;
    store.applyFrame(mutation([{ kind: "set", path: ["messages", 0, "parts", 0, "status"], value: "future-status" }]));
    expect(() => store.completeHistoryFetch(fetch, { messageOffset: 0, messages: [assistantMessage("earlier", 1, [validTool()])] })).toThrow("status");
    expect(store.getSnapshot().state).toBe(initial);
  });

  it("混合帧被拒后，不把其中的历史增量留在翻页缓冲中", () => {
    const store = new TransportFrameStore(transportState({ messageOffset: 1, messages: [assistantMessage("later", 1, [validTool()])] }));
    const fetch = store.beginHistoryFetch()!;
    expect(() => store.applyFrame(mutation([
      { kind: "set", path: ["messages", 0, "parts", 0, "intent"], value: "被拒帧的内容" },
      { kind: "set", path: ["messages", 1, "parts", 0, "status"], value: "future-status" },
    ]))).toThrow("status");
    const earlier = assistantMessage("earlier", 1, [toolCallPart("earlier-call", "read_file")]);
    expect(store.completeHistoryFetch(fetch, { messageOffset: 0, messages: [earlier] })).toBe(true);
    expect(store.getSnapshot().state.messages[0]).toBe(earlier);
  });
});

const command = (text: string): UserAddMessageCommand => ({
  type: "add-message",
  message: { role: "user", parts: [{ type: "text", text }] },
});

/** 接管宿主动画帧：回调留给测试手动触发（由 afterEach 还原全局）。 */
const deferredScheduler = () => {
  const callbacks: FrameRequestCallback[] = [];
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => callbacks.push(callback));
  vi.stubGlobal("cancelAnimationFrame", () => undefined);
  return { callbacks };
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
  afterEach(() => vi.unstubAllGlobals());

  it("publishes one canonical layout shared by conversion and preserves tool projections on text append", () => {
    const store = new TransportFrameStore(running([assistantMessage("a", 1, [toolCallPart("c", "read_file"), { type: "text", text: "正文" }])]));
    const before = store.getSnapshot().items[0];
    if (before.kind !== "canonical") throw new Error("Expected canonical message");
    expect(convertFrameStoreItem(before).metadata?.custom?.turnLayout).toBe(before.turnLayout);
    store.applyFrame(mutation([{ kind: "append-text", path: ["messages", 0, "parts", 1, "text"], value: "继续" }]));
    store.flushScheduledPublication();
    const next = store.getSnapshot().items[0];
    if (next.kind !== "canonical") throw new Error("Expected canonical message");
    expect(next.turnLayout).not.toBe(before.turnLayout);
    expect(next.turnLayout?.blocks[0]).toBe(before.turnLayout?.blocks[0]);
    expect(convertFrameStoreItem(next).metadata?.custom?.turnLayout).toBe(next.turnLayout);
  });

  it("正文更新保留排队和乐观消息的包装身份，队列内容/投递方式变化仍刷新", () => {
    const store = new TransportFrameStore({ ...running([streaming("active", "正文")]), queued_messages: [queuedMessage("q1")] });
    const pending = command("尚未确认");
    store.setPendingCommands([pending]);
    const before = store.getSnapshot();
    store.applyFrame(mutation([{ kind: "append-text", path: ["messages", 0, "parts", 0, "text"], value: "继续" }]));
    store.flushScheduledPublication();
    expect(store.getSnapshot().items[1]).toBe(before.items[1]);
    expect(store.getSnapshot().items[2]).toBe(before.items[2]);

    store.applyFrame(mutation([
      { kind: "set", path: ["queued_messages", 0, "parts", 0, "text"], value: "修改后的排队内容" },
      { kind: "set", path: ["queued_messages", 0, "delivery"], value: "immediate" },
    ]));
    const changed = store.getSnapshot();
    expect(changed.items[1]).not.toBe(before.items[1]);
    expect(convertFrameStoreItem(changed.items[1])).toMatchObject({ content: [{ type: "text", text: "修改后的排队内容" }] });
    expect(changed.state.queued_messages?.[0].delivery).toBe("immediate");
    expect(changed.items[2]).toBe(before.items[2]);

    store.applyFrame(mutation([{ kind: "set", path: ["queued_messages"], value: [] }]));
    expect(store.getSnapshot().items).toEqual([changed.items[0], before.items[2]]);
    store.clearPendingCommand(pending);
    expect(store.getSnapshot().items).toEqual([changed.items[0]]);
  });

  it("重排队列复用各条身份，新确认消息替换乐观包装，完整快照更新不读旧缓存", () => {
    const q1 = queuedMessage("q1"), q2 = queuedMessage("q2");
    const store = new TransportFrameStore({ ...running([streaming("active", "正文")]), queued_messages: [q1, q2] });
    const before = store.getSnapshot().items;
    store.applyFrame(mutation([{ kind: "set", path: ["queued_messages"], value: [q2, q1] }]));
    expect(store.getSnapshot().items).toEqual([before[0], before[2], before[1]]);
    expect(store.getSnapshot().items[1]).toBe(before[2]);
    store.setPendingCommands([command("新消息")]);
    store.applyFrame(mutation([{ kind: "set", path: ["queued_messages", 2], value: queuedMessage("q3", "新消息") }]));
    expect(store.getSnapshot().pendingCommands).toHaveLength(0);
    expect(store.getSnapshot().items).toHaveLength(4);
    store.applyFrame(full({ ...running([streaming("active", "重连正文")]), queued_messages: [queuedMessage("q1", "重连内容")] }));
    expect(convertFrameStoreItem(store.getSnapshot().items[1])).toMatchObject({ id: "q1", content: [{ text: "重连内容" }] });
  });

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
    const store = new TransportFrameStore(running([...historical, streaming("active", "partial")]));
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
    const store = new TransportFrameStore(running([streaming("history", "old"), streaming("active", "partial")]));
    let publicationCount = 0;
    store.subscribe(() => { publicationCount += 1; });

    store.applyFrame(mutation([{ kind: "append-text", path: ["messages", 0, "parts", 0, "text"], value: " changed" }]));

    expect(publicationCount).toBe(1);
    expect(scheduler.callbacks).toHaveLength(0);
  });

  it("reports frame and publication timings through the optional development probe", () => {
    const performanceProbe = probe();
    const scheduler = deferredScheduler();
    const store = new TransportFrameStore(running([streaming("active", "partial")]), { performanceProbe });

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
    const cleanup = command("cleanup");
    store.setPendingCommands([cleanup]);
    store.clearPendingCommand(cleanup);
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
    store.setPendingCommands([command("first"), command("second")]);

    store.applyFrame(mutation([{ kind: "set", path: ["activity"], value: activity(2, "running") }]));
    expect(store.getSnapshot().pendingCommands).toHaveLength(2);

    store.applyFrame(mutation([{ kind: "set", path: ["messages", 1], value: userMessage("2", 2, "first") }]));
    expect(store.getSnapshot().pendingCommands.map((item) => item.message.parts[0])).toEqual([{ type: "text", text: "second" }]);

    store.applyFrame(mutation([{ kind: "set", path: ["queued_messages"], value: [queuedMessage("3", "second")] }]));
    expect(store.getSnapshot().pendingCommands).toEqual([]);
  });

  it("Agent 消息到达不认领用户的乐观发送：它不是用户发的", () => {
    const store = new TransportFrameStore(transportState({ messages: [userMessage("1", 1)], activity: activity(1, "completed") }));
    store.setPendingCommands([command("mine")]);

    store.applyFrame(mutation([{ kind: "set", path: ["messages", 1], value: agentMessage("2", 2) }]));
    expect(store.getSnapshot().pendingCommands).toHaveLength(1);

    store.applyFrame(mutation([{ kind: "set", path: ["messages", 2], value: userMessage("3", 2, "mine") }]));
    expect(store.getSnapshot().pendingCommands).toEqual([]);
  });

  it("clears only the rejected command pending projection", () => {
    const store = new TransportFrameStore(transportState());
    const first = command("first");
    const second = command("second");
    store.setPendingCommands([first, second]);
    store.clearPendingCommand(first);
    expect(store.getSnapshot().pendingCommands).toEqual([second]);
  });
});

describe("history window", () => {
  // 完整历史：u0 a0 u1 a1 u2 a2；窗口只带最后一轮（messageOffset 4）。
  const history = [
    userMessage("u0", 1), assistantMessage("a0", 1),
    userMessage("u1", 2), assistantMessage("a1", 2),
    userMessage("u2", 3), assistantMessage("a2", 3),
  ];
  const windowed = () => transportState({ messages: history.slice(4), messageOffset: 4, activity: activity(3, "completed") });
  const texts = (store: TransportFrameStore) => store.getSnapshot().state.messages.map((message) => (
    message.parts[0]?.type === "text" ? message.parts[0].text : ""
  ));
  const setText = (index: number, text: string) => ({ kind: "set" as const, path: ["messages", index, "parts", 0, "text"], value: text });

  it("增量按完整历史的位置换算到本地；指向未加载消息的增量丢弃", () => {
    const store = new TransportFrameStore(windowed());
    store.applyFrame(mutation([setText(5, "new a2"), setText(1, "lost a0")]));
    expect(texts(store)).toEqual(["hello", "new a2"]);
    expect(store.getSnapshot().state.messageOffset).toBe(4);
  });

  it("翻页插到前面并回放请求期间落在页内的增量", () => {
    const store = new TransportFrameStore(windowed());
    const fetch = store.beginHistoryFetch();
    expect(fetch).toEqual({ generation: expect.any(Number), end: 4 });
    expect(store.beginHistoryFetch()).toBeNull();
    store.applyFrame(mutation([setText(3, "late a1")]));

    expect(store.completeHistoryFetch(fetch!, { messages: history.slice(2, 4), messageOffset: 2 })).toBe(true);
    expect(store.getSnapshot().state.messageOffset).toBe(2);
    expect(store.getSnapshot().state.messages.map((message) => message.id)).toEqual(["u1", "a1", "u2", "a2"]);
    expect(texts(store)[1]).toBe("late a1");
    // 插入后，增量继续按新的本地位置落下。
    store.applyFrame(mutation([setText(2, "u1 edited")]));
    expect(texts(store)[0]).toBe("u1 edited");
  });

  it("完整快照替换窗口后，在途的翻页结果作废", () => {
    const store = new TransportFrameStore(windowed());
    const fetch = store.beginHistoryFetch()!;
    store.applyFrame(full(windowed()));
    expect(store.completeHistoryFetch(fetch, { messages: history.slice(2, 4), messageOffset: 2 })).toBe(false);
    expect(store.getSnapshot().state.messageOffset).toBe(4);
  });

  it("与已加载部分不衔接的页丢弃，之后可以重新翻", () => {
    const store = new TransportFrameStore(windowed());
    const fetch = store.beginHistoryFetch()!;
    expect(store.completeHistoryFetch(fetch, { messages: history.slice(0, 2), messageOffset: 0 })).toBe(false);
    expect(store.beginHistoryFetch()).not.toBeNull();
  });

  it("插入的旧页与起点更早的新窗口里的用户消息，都不抵消乐观命令", () => {
    const store = new TransportFrameStore(windowed());
    store.setPendingCommands([command("pending")]);
    const fetch = store.beginHistoryFetch()!;
    store.completeHistoryFetch(fetch, { messages: history.slice(2, 4), messageOffset: 2 });
    expect(store.getSnapshot().pendingCommands).toHaveLength(1);

    // 编辑重跑移出消息后，新窗口从更早处起：u0 只是之前没加载，不是新出现的。
    store.applyFrame(full(transportState({ messages: history.slice(0, 4), messageOffset: 0, activity: activity(3, "completed") })));
    expect(store.getSnapshot().pendingCommands).toHaveLength(1);
    store.applyFrame(full(transportState({
      messages: [...history.slice(0, 4), userMessage("u-new", 4)], messageOffset: 0, activity: activity(3, "completed"),
    })));
    expect(store.getSnapshot().pendingCommands).toHaveLength(0);
  });
});
