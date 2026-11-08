import type { TransportMessage, TransportToolCallPart } from "../contract";

export type ToolCalls = ReadonlyMap<string, TransportToolCallPart>;
const messageCalls = new WeakMap<TransportMessage, readonly TransportToolCallPart[]>();

/** canonical message 不可变；正文新版本只提取该消息，未变历史不再读取 parts。 */
function callsOf(message: TransportMessage): readonly TransportToolCallPart[] {
  let calls = messageCalls.get(message);
  if (!calls) {
    calls = message.parts.filter((part): part is TransportToolCallPart => part.type === "tool-call");
    messageCalls.set(message, calls);
  }
  return calls;
}

/**
 * 已加载窗口的工具索引，由 frame store 持有。内容增量更新单条消息；完整替换/翻页重建成员关系。
 * 只派生 canonical part 引用，不保存工具状态副本；发布过的 Map 永不修改。
 */
export class ToolCallIndex {
  private messages = new Map<string, readonly TransportToolCallPart[]>();
  private calls: ToolCalls = new Map();

  get snapshot(): ToolCalls { return this.calls; }

  replace(messages: readonly TransportMessage[]): void {
    const members = new Map<string, readonly TransportToolCallPart[]>();
    const calls = new Map<string, TransportToolCallPart>();
    for (const message of messages) {
      const parts = callsOf(message);
      members.set(message.id, parts);
      for (const part of parts) calls.set(part.toolCallId, part);
    }
    this.messages = members;
    if (calls.size !== this.calls.size || [...calls].some(([id, part]) => this.calls.get(id) !== part)) this.calls = calls;
  }

  update(message: TransportMessage): void {
    const previous = this.messages.get(message.id) ?? [];
    const next = callsOf(message);
    this.messages.set(message.id, next);
    if (previous.length === next.length && next.every((part, i) => part === previous[i])) return;
    const calls = new Map(this.calls);
    for (const part of previous) calls.delete(part.toolCallId);
    for (const part of next) calls.set(part.toolCallId, part);
    this.calls = calls;
  }
}
