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
const table = (rows: number) => ["| 名称 | 说明 |", "| --- | --- |", ...Array.from({ length: rows }, (_, row) => `| 项${row} | 描述${row} |`)].join("\n");

describe("estimateMessageHeight", () => {
  // 期望值是 `max-w-3xl` 列宽下单一特性消息的实测高度。
  it("用户消息：气泡按折行增高，附件区按行排在气泡上方", () => {
    expect(estimateMessageHeight(user(text("问题")))).toBe(96);
    expect(estimateMessageHeight(user(text("请帮我看看。".repeat(20))))).toBe(142);
    expect(estimateMessageHeight(user(text("看这个"), file))).toBe(134);
    expect(estimateMessageHeight(user(text("看这些"), file, file, file))).toBe(134);
    expect(estimateMessageHeight(user(image))).toBe(183);
  });

  it("回答：标题按级别、列表项、表格行、代码行分别计高", () => {
    const base = estimateMessageHeight(assistant(text("一段话")));
    expect(base).toBe(100);
    expect(estimateMessageHeight(assistant(text("# 标题\n\n一段话")))).toBe(148);
    expect(estimateMessageHeight(assistant(text("### 标题\n\n一段话")))).toBe(140);
    expect(estimateMessageHeight(assistant(text("- 一\n- 二\n- 三")))).toBe(172);
    expect(estimateMessageHeight(assistant(text(table(6)))) - estimateMessageHeight(assistant(text(table(3))))).toBe(3 * 37);
    expect(estimateMessageHeight(assistant(text(code(15)))) - estimateMessageHeight(assistant(text(code(5))))).toBe(205);
  });

  it("相邻的思考过程与工具调用合成一组追踪行", () => {
    const traced = estimateMessageHeight(assistant(
      { type: "reasoning", text: "思考" },
      { type: "tool-call", toolCallId: "c", toolName: "read_file", args: {} },
      text("一段话"),
    ));
    expect(traced - estimateMessageHeight(assistant(text("一段话")))).toBe(36 + 12 + 28);
  });

  it("流式中未闭合的代码块也计入", () => {
    const open = estimateMessageHeight(assistant(text("```ts\nconst a = 1;\nconst b = 2;")));
    expect(open).toBeGreaterThan(estimateMessageHeight(assistant(text(""))) + 60);
  });

  it("消息未知时给出回答的最小高度", () => {
    expect(estimateMessageHeight(undefined)).toBeGreaterThan(0);
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

  it("Task 删除后丢弃其行高", () => {
    rememberMessageHeight(1, 720, "m1", 640);
    forgetMessageHeights([1]);
    expect(rememberedMessageHeight(1, 720, "m1")).toBeUndefined();
  });
});
