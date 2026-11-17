import { describe, expect, it } from "vitest";

import type { TransportMessagePart, TransportState } from "@/lib/assistant/contract";
import { deliverableWatchKey } from "@/components/conversation/tools/reply-presentation/use-session-deliverable-watch";
import { assistantMessage, deliveredFile, toolCallPart, transportState } from "./transport-fixtures";

const present = (paths: string[]) =>
  toolCallPart("p", "present_deliverables", { status: "completed", presentation: "quiet", deliverables: paths.map((path) => deliveredFile(path)) });
const stateWith = (parts: TransportMessagePart[]): TransportState => transportState({ messages: [assistantMessage("assistant-turn-1", 1, parts)] });

// 目的：会话登记的路径集合只在出现新交付物时变化；潜在缺陷：流式增量每帧都换快照，键跟着变，监听反复重连并整体失效文件查询。
describe("deliverableWatchKey", () => {
  it("正文流式追加时键不变，来了新交付物才变", () => {
    const first = deliverableWatchKey(stateWith([present(["a.md"]), { type: "text", text: "写" }]));
    const streamed = deliverableWatchKey(stateWith([present(["a.md"]), { type: "text", text: "写好了" }]));
    expect(streamed).toBe(first);
    expect(deliverableWatchKey(stateWith([present(["a.md", "b.md"])]))).not.toBe(first);
  });

  it("同一个快照对象只计算一次", () => {
    const state = stateWith([present(["a.md"])]);
    expect(deliverableWatchKey(state)).toBe(deliverableWatchKey(state));
    expect(JSON.parse(deliverableWatchKey(state))).toEqual(["a.md"]);
  });
});
