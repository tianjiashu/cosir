import { describe, expect, it } from "vitest";
import { forgetDisclosureHeight, rememberMessageHeight, rememberedMessageHeight } from "@/lib/assistant/turn/message-height-cache";

const big = (id: string, n: number) => {
  const content = Array.from({ length: n }, (_, i) => ({ type: "tool-call", toolCallId: `c${i}`, toolName: "read_file", args: {}, argsText: "{}", result: "x".repeat(20000) }));
  return { id, role: "assistant", content, status: { type: "complete" }, metadata: { custom: { turnLayout: { blocks: content.map((part, index) => ({ kind: "standalone", index, part })) } } } } as never;
};

describe("R2/R3", () => {
  it("R2 无条目也序列化；同对象二次命中缓存", () => {
    const m = big("m1", 50);
    let t = performance.now();
    expect(rememberedMessageHeight(1, 500, "m1", m)).toBeUndefined();
    const first = performance.now() - t;
    t = performance.now();
    for (let i = 0; i < 1000; i++) rememberedMessageHeight(1, 500, "m1", m);
    const repeated = performance.now() - t;
    const t2 = performance.now();
    const len = JSON.stringify([ (m as { content: unknown }).content, null, (m as { metadata: { custom: unknown } }).metadata.custom ]).length;
    console.log(`first=${first.toFixed(2)}ms repeated1000=${repeated.toFixed(2)}ms jsonLen=${len} stringifyOnce=${(performance.now() - t2).toFixed(2)}ms`);
  });
  it("R3 回合内来信与终端命令键不失效", () => {
    const m = { id: "m2", role: "assistant", content: [{ type: "tool-call", toolCallId: "c9" }], status: { type: "complete" }, metadata: { custom: {} } } as never;
    rememberMessageHeight(2, 500, "m2", 321, m);
    forgetDisclosureHeight(2, "agent:part-xyz");
    forgetDisclosureHeight(2, "terminal-command:c9");
    forgetDisclosureHeight(2, "terminal-reader:c9");
    expect(rememberedMessageHeight(2, 500, "m2", m)).toBe(321);
    forgetDisclosureHeight(2, "call:c9");
    expect(rememberedMessageHeight(2, 500, "m2", m)).toBeUndefined();
  });
});
