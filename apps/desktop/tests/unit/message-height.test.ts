import { outcome } from "./transport-fixtures";
import { useDisclosureStore } from "@/lib/assistant/disclosure-store";
import type { ThreadMessage } from "@assistant-ui/react";
import { afterEach, describe, expect, it } from "vitest";

import { forgetMessageHeights, rememberMessageHeight, rememberedMessageHeight } from "@/lib/assistant/message-height-cache";
import { estimateMessageHeight } from "@/lib/assistant/message-height-estimate";

const assistant = (...content: object[]) => ({ id: "a", role: "assistant", content }) as unknown as ThreadMessage;
const code = (lines: number) => ["```ts", ...Array.from({ length: lines }, (_, line) => `const v${line} = ${line};`), "```"].join("\n");
const user = (...content: object[]) => ({ id: "u", role: "user", content }) as unknown as ThreadMessage;
const text = (value: string) => ({ type: "text", text: value });
const file = { type: "file", filename: "a.md", mimeType: "text/markdown", data: "" };
const image = { type: "image", image: "x" };
const call = (id: string, backendStatus = "completed", artifact: Record<string, unknown> = {}) => ({presentation: "activity",
  type: "tool-call", toolCallId: id, toolName: "read_file", args: {}, artifact: {presentation: "activity",  backendStatus, awaitingUser: false, ...artifact },
});
const running = (...content: object[]) => ({ id: "a", role: "assistant", content, status: { type: "running" } }) as unknown as ThreadMessage;
const table = (rows: number) => ["| 名称 | 说明 |", "| --- | --- |", ...Array.from({ length: rows }, (_, row) => `| 项${row} | 描述${row} |`)].join("\n");

describe("estimateMessageHeight", () => {
  it("completed replies without duration/process reserve no heading, while real zero duration does", () => {
    const withDuration = (durationMs: number | null) => ({ ...assistant(text("回复")), metadata: { custom: { outcome: outcome("completed", { durationMs }) } } }) as unknown as ThreadMessage;
    expect(estimateMessageHeight(withDuration(0)) - estimateMessageHeight(withDuration(null))).toBe(37 + 16);
  });

  it("用户消息：气泡按折行增高，附件区按行排在气泡上方", () => {
    expect(estimateMessageHeight(user(text("问题")))).toBe(100);
    expect(estimateMessageHeight(user(text("请帮我看看。".repeat(20))))).toBe(146);
    expect(estimateMessageHeight(user(text("看这个"), file))).toBe(138);
    expect(estimateMessageHeight(user(text("看这些"), file, file, file))).toBe(138);
    expect(estimateMessageHeight(user(image))).toBe(149);
  });

  // 目的：估计与渲染共用收叠阈值；潜在缺陷：多图消息已收成一叠，估计仍按逐行铺开算，向上滚动时被逐行修正数百像素。
  it("用户消息：阈值以内的图片逐行铺开，超过阈值按收起的一叠计高", () => {
    const images = (count: number) => user(...Array.from({ length: count }, () => image));
    expect(estimateMessageHeight(images(5))).toBe(149 + 104);
    expect(estimateMessageHeight(images(6))).toBe(149);
    expect(estimateMessageHeight(images(20))).toBe(149);
  });

  it("回答：标题按级别、列表项、表格行、代码行分别计高", () => {
    const base = estimateMessageHeight(assistant(text("一段话")));
    expect(base).toBe(132);
    expect(estimateMessageHeight(assistant(text("# 标题\n\n一段话")))).toBe(180);
    expect(estimateMessageHeight(assistant(text("### 标题\n\n一段话")))).toBe(172);
    expect(estimateMessageHeight(assistant(text("- 一\n- 二\n- 三")))).toBe(212);
    expect(estimateMessageHeight(assistant(text(table(6)))) - estimateMessageHeight(assistant(text(table(3))))).toBe(3 * 37);
    expect(estimateMessageHeight(assistant(text(code(15)))) - estimateMessageHeight(assistant(text(code(5))))).toBe(205);
  });

  it("completed process folding uses the same outcome rule as rendering", () => {
    const content = [text("很长的进展。".repeat(100)), call("c1"), text("回复")];
    const complete = { ...assistant(...content), metadata: { custom: { outcome: outcome("completed") } } } as unknown as ThreadMessage;
    const stopped = { ...complete, metadata: { custom: { outcome: outcome("cancelled") } } } as unknown as ThreadMessage;
    expect(estimateMessageHeight(stopped)).toBeGreaterThan(estimateMessageHeight(complete) + 200);
    useDisclosureStore.getState().set(8, "process:a", true);
    expect(estimateMessageHeight(complete, 8)).toBeGreaterThan(estimateMessageHeight(complete));
    useDisclosureStore.getState().clearTask(8);
  });

  it("compresses successful calls but budgets active controls and all progress prose", () => {
    const calls = Array.from({ length: 20 }, (_, i) => call(`c${i}`));
    expect(estimateMessageHeight(running(...calls))).toBe(estimateMessageHeight(running(call("c"))));
    expect(estimateMessageHeight(running(...calls, call("live", "running")))).toBeGreaterThan(estimateMessageHeight(running(...calls)));
    expect(estimateMessageHeight(running(text("进展".repeat(100)), ...calls))).toBeGreaterThan(estimateMessageHeight(running(...calls)) + 100);
  });

  it("artifact and decision heights are independent of folded history", () => {
    const plain = assistant(call("c"));
    const waiting = assistant(call("c", "pending", { awaitingUser: true }));
    expect(estimateMessageHeight(waiting)).toBeGreaterThan(estimateMessageHeight(plain) + 96);
    const changes = (paths: string[]) => assistant(call("w", "completed", { display_data: { kind: "file-changes", changes: paths.map((path) => ({ path, status: "modified" })) } }));
    expect(estimateMessageHeight(changes(["a", "b", "c"])) - estimateMessageHeight(changes(["a"]))).toBe(56);
  });

  it("long Agent input starts compact and only grows on explicit disclosure", () => {
    const msg = { ...user(text("内容".repeat(1000))), metadata: { custom: { source: { taskId: 2, name: "审查" } } } } as unknown as ThreadMessage;
    expect(estimateMessageHeight(msg, 8)).toBeLessThan(100);
    useDisclosureStore.getState().set(8, "agent:u", true);
    expect(estimateMessageHeight(msg, 8)).toBeGreaterThan(500);
    useDisclosureStore.getState().clearTask(8);
  });

});

describe("message height cache", () => {
  afterEach(() => forgetMessageHeights([1, 2]));

  it("按 Task 与列宽记住实测行高，列宽变了整桶作废", () => {
    rememberMessageHeight(1, 720, "m1", 640);
    rememberMessageHeight(1, 720, "m2", 96);
    expect(rememberedMessageHeight(1, 720, "m1")).toBe(640);
    expect(rememberedMessageHeight(2, 720, "m1")).toBeUndefined();
    // 列表尚未布局时信任记下的列宽。
    expect(rememberedMessageHeight(1, null, "m2")).toBe(96);
    expect(rememberedMessageHeight(1, 600, "m1")).toBeUndefined();

    rememberMessageHeight(1, 600, "m1", 700);
    expect(rememberedMessageHeight(1, 600, "m1")).toBe(700);
    expect(rememberedMessageHeight(1, 600, "m2")).toBeUndefined();
  });

  it("equal snapshots retain measurements, changed content and disclosure invalidate only their own row", () => {
    const first = assistant(text("最初正文"));
    rememberMessageHeight(1, 720, "a", 200, first);
    expect(rememberedMessageHeight(1, 720, "a", structuredClone(first))).toBe(200);
    expect(rememberedMessageHeight(1, 720, "a", assistant(text("后来增长的正文")))).toBeUndefined();
    rememberMessageHeight(1, 720, "other", 96);
    useDisclosureStore.getState().set(1, "process:a", true);
    expect(rememberedMessageHeight(1, 720, "a", first)).toBeUndefined();
    expect(rememberedMessageHeight(1, 720, "other")).toBe(96);
    useDisclosureStore.getState().clearTask(1);
  });

  it("Task 删除后丢弃其行高", () => {
    rememberMessageHeight(1, 720, "m1", 640);
    forgetMessageHeights([1]);
    expect(rememberedMessageHeight(1, 720, "m1")).toBeUndefined();
  });
});
