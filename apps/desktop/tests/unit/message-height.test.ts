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
const call = (id: string, backendStatus = "completed", artifact: Record<string, unknown> = {}) => ({
  type: "tool-call", toolCallId: id, toolName: "read_file", args: {}, artifact: { backendStatus, awaitingUser: false, ...artifact },
});
const running = (...content: object[]) => ({ id: "a", role: "assistant", content, status: { type: "running" } }) as unknown as ThreadMessage;
const table = (rows: number) => ["| 名称 | 说明 |", "| --- | --- |", ...Array.from({ length: rows }, (_, row) => `| 项${row} | 描述${row} |`)].join("\n");

describe("estimateMessageHeight", () => {
  // 期望值是 `max-w-3xl` 列宽下单一特性消息的实测高度。
  it("用户消息：气泡按折行增高，附件区按行排在气泡上方", () => {
    expect(estimateMessageHeight(user(text("问题")))).toBe(96);
    expect(estimateMessageHeight(user(text("请帮我看看。".repeat(20))))).toBe(142);
    expect(estimateMessageHeight(user(text("看这个"), file))).toBe(134);
    expect(estimateMessageHeight(user(text("看这些"), file, file, file))).toBe(134);
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
    expect(base).toBe(112);
    expect(estimateMessageHeight(assistant(text("# 标题\n\n一段话")))).toBe(160);
    expect(estimateMessageHeight(assistant(text("### 标题\n\n一段话")))).toBe(152);
    expect(estimateMessageHeight(assistant(text("- 一\n- 二\n- 三")))).toBe(184);
    expect(estimateMessageHeight(assistant(text(table(6)))) - estimateMessageHeight(assistant(text(table(3))))).toBe(3 * 37);
    expect(estimateMessageHeight(assistant(text(code(15)))) - estimateMessageHeight(assistant(text(code(5))))).toBe(205);
  });

  // 回合按 `projectTurn` 分区：结束后过程区收成一行区头；只有 ≤1 次调用时平铺每项一行。
  it("回答：结束后多次调用的过程区收成一行区头，单次调用平铺", () => {
    const answer = estimateMessageHeight(assistant(text("一段话")));
    const collapsed = estimateMessageHeight(assistant(
      { type: "reasoning", text: "思考" }, call("c1"), call("c2"), text("一段话"),
    ));
    expect(collapsed - answer).toBe(28 + 12);
    const flat = estimateMessageHeight(assistant({ type: "reasoning", text: "思考" }, call("c1"), text("一段话")));
    expect(flat - answer).toBe(28 + 28 + 12);
    // 思考与一次调用各一行，共用一段区间距。
    expect(estimateMessageHeight(assistant(call("c1"), text("一段话"))) - answer).toBe(28 + 12);
  });

  // 目的：运行中过程区展开但只露活跃尾部，长回合估计有上界；潜在缺陷：按全部过程项计高，向上滚动时修正量随回合长度无界增长。
  it("回答：运行中过程区展开，只按活跃尾部（最多 4 项）加「更早」一行计高", () => {
    const base = estimateMessageHeight(running(call("c1", "running")));
    // 单次调用平铺：一行调用 + 区间距。
    expect(base).toBe(24 + 40 + 28 + 12);
    const parts = Array.from({ length: 7 }, (_, index) => call(`c${index}`, index === 6 ? "running" : "completed"));
    const long = estimateMessageHeight(running(...parts));
    // 区头 + 「前面还有 N 步」一行 + 尾部 4 行，再减去 base 里的那一行。
    expect(long - base).toBe(28 + 24 + 4 * 28 - 28);
    expect(estimateMessageHeight(running(...parts, call("c8", "pending")))).toBe(long);
  });

  it("回答：改动卡按文件行计高，等待用户的调用另加一张提示卡", () => {
    const answer = estimateMessageHeight(assistant(text("一段话")));
    const changes = (id: string, paths: string[]) => call(id, "completed", {
      display_data: { kind: "file-changes", changes: paths.map((path) => ({ path, status: "modified" })) },
    });
    const one = estimateMessageHeight(assistant(changes("w1", ["a"]), text("一段话")));
    const three = estimateMessageHeight(assistant(changes("w1", ["a", "b"]), changes("w2", ["a", "c"]), text("一段话")));
    // 平铺的单次调用一行 + 改动卡（卡头 + 文件行）。
    expect(one - answer).toBe(28 + 12 + 40 + 28 + 12);
    // 两次调用收成区头（与单行同高），改动卡按合并后的 3 个文件计。
    expect(three - one).toBe(2 * 28);
    const waiting = estimateMessageHeight(assistant(call("h1", "pending", { awaitingUser: true })));
    expect(waiting).toBe(24 + 40 + 28 + 12 + 96 + 12);
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
