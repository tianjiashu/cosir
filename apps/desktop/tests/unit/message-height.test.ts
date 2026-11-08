import { outcome, assistantMessage, toolCallPart, activity } from "./transport-fixtures";
import { useDisclosureStore } from "@/lib/assistant/disclosure-store";
import type { ThreadMessage } from "@assistant-ui/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { toThreadMessageWithRenderContext } from "@/lib/assistant/transport-message-converter";
import type { TransportMessagePart, TransportOutcome } from "@/lib/assistant/contract";
import * as turnLayout from "@/lib/assistant/turn-layout";

import { forgetMessageHeights, rememberMessageHeight, rememberedMessageHeight } from "@/lib/assistant/message-height-cache";
import { estimateMessageHeight } from "@/lib/assistant/message-height-estimate";

const projected = (parts: TransportMessagePart[], end: TransportOutcome | null = null, live = false) => toThreadMessageWithRenderContext(assistantMessage("a", 1, parts, end), { runId: 1, source: null, outcome: end, isRunEnd: true, activity: live ? activity(1, "running") : null }) as ThreadMessage;
const assistant = (...parts: TransportMessagePart[]) => projected(parts);
const code = (lines: number) => ["```ts", ...Array.from({ length: lines }, (_, line) => `const v${line} = ${line};`), "```"].join("\n");
const user = (...content: object[]) => ({ id: "u", role: "user", content }) as unknown as ThreadMessage;
const text = (value: string, finalReply = true) => ({ type: "text" as const, text: value, finalReply });
const file = { type: "file", filename: "a.md", mimeType: "text/markdown", data: "" };
const image = { type: "image", image: "x" };
const call = (id: string, status: "completed" | "pending" | "running" = "completed", extra = {}) => toolCallPart(id, "read_file", { status, ...extra });
const running = (...parts: TransportMessagePart[]) => projected(parts, null, true);
const table = (rows: number) => ["| 名称 | 说明 |", "| --- | --- |", ...Array.from({ length: rows }, (_, row) => `| 项${row} | 描述${row} |`)].join("\n");

describe("estimateMessageHeight", () => {
  it("传 Task 时也复用内容投影，开合、跨 Task 和新消息版本仍改变估高", () => {
    const project = vi.spyOn(turnLayout, "projectTurn");
    try {
      const parts = [text("进展。".repeat(150), false), call("c1"), call("c2"), text("最终回复")];
      const msg = projected(parts, outcome("completed"));
      const folded = estimateMessageHeight(msg, 801);
      expect(estimateMessageHeight(msg, 801)).toBe(folded);
      useDisclosureStore.getState().set(801, "process:a", true);
      const open = estimateMessageHeight(msg, 801);
      expect(open).toBeGreaterThan(folded);
      useDisclosureStore.getState().set(801, "activity:a:interval:1", true);
      expect(estimateMessageHeight(msg, 801)).toBeGreaterThan(open);
      expect(estimateMessageHeight(msg, 802)).toBe(folded);
      useDisclosureStore.getState().reset(801, "process:a");
      expect(estimateMessageHeight(msg, 801)).toBe(folded);
      expect(project).toHaveBeenCalledTimes(1);
      const changed = projected([...parts, text("新增正文。".repeat(200))], outcome("completed"));
      expect(estimateMessageHeight(changed, 801)).toBeGreaterThan(folded);
      expect(project).toHaveBeenCalledTimes(2);
    } finally {
      project.mockRestore();
      useDisclosureStore.getState().clearTask(801);
      useDisclosureStore.getState().clearTask(802);
    }
  });

  it("completed replies never reserve a duration-only heading; process and failures still reserve it", () => {
    const withDuration = (durationMs: number) => projected([text("回复")], outcome("completed", { durationMs }));
    const plainHeight = estimateMessageHeight(withDuration(0));
    for (const duration of [0, 129000]) expect(estimateMessageHeight(withDuration(duration))).toBe(plainHeight);
    const done = outcome("completed", { durationMs: 129000 });
    expect(estimateMessageHeight(projected([call("quiet", "completed", { presentation: "quiet" }), text("回复")], done))).toBe(plainHeight);
    expect(estimateMessageHeight(projected([call("read"), text("回复")], done)) - plainHeight).toBe(37 + 16);
    expect(estimateMessageHeight(projected([text("回复")], outcome("failed"))) - plainHeight).toBe(37 + 16);
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
    const content = [text("很长的进展。".repeat(100), false), call("c1"), text("回复")];
    const complete = projected(content, outcome("completed"));
    const stopped = projected(content, outcome("cancelled"));
    expect(estimateMessageHeight(stopped)).toBeGreaterThan(estimateMessageHeight(complete) + 200);
    useDisclosureStore.getState().set(8, "process:a", true);
    expect(estimateMessageHeight(complete, 8)).toBeGreaterThan(estimateMessageHeight(complete));
    useDisclosureStore.getState().clearTask(8);
  });

  it("compresses completed and active calls while budgeting all progress prose", () => {
    const calls = Array.from({ length: 20 }, (_, i) => call(`c${i}`));
    expect(estimateMessageHeight(running(...calls))).toBe(estimateMessageHeight(running(call("c"))));
    expect(estimateMessageHeight(running(...calls, call("live", "running")))).toBe(estimateMessageHeight(running(...calls)));
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
