import { outcome, assistantMessage, toolCallPart, activity, deliveredFile } from "./transport-fixtures";
import { useDisclosureStore } from "@/lib/assistant/turn/disclosure-store";
import type { ThreadMessage } from "@assistant-ui/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { toThreadMessageWithRenderContext } from "@/lib/assistant/transport-message-converter";
import type { TransportMessagePart, TransportOutcome } from "@/lib/assistant/contract";
import * as turnLayout from "@/lib/assistant/turn/turn-layout";

import { forgetMessageHeights, rememberMessageHeight, rememberedMessageHeight } from "@/lib/assistant/turn/message-height-cache";
import { estimateMessageHeight } from "@/lib/assistant/turn/message-height-estimate";

const projected = (parts: TransportMessagePart[], end: TransportOutcome | null = null, live = false) => toThreadMessageWithRenderContext(assistantMessage("a", 1, parts, end), { turn: 1, inherited: false, source: null, outcome: end, activity: live ? activity(1, "running") : null }) as ThreadMessage;
const assistant = (...parts: TransportMessagePart[]) => projected(parts);
const code = (lines: number) => ["```ts", ...Array.from({ length: lines }, (_, line) => `const v${line} = ${line};`), "```"].join("\n");
const user = (...content: object[]) => ({ id: "u", role: "user", content }) as unknown as ThreadMessage;
const text = (value: string, finalReply = true) => ({ type: "text" as const, text: value, finalReply });
const image = { type: "image", image: "x" };
const call = (id: string, status: "completed" | "pending" | "running" = "completed", extra = {}) => toolCallPart(id, "read_file", { status, ...extra });
const running = (...parts: TransportMessagePart[]) => projected(parts, null, true);
const table = (rows: number) => ["| 名称 | 说明 |", "| --- | --- |", ...Array.from({ length: rows }, (_, row) => `| 项${row} | 描述${row} |`)].join("\n");

describe("estimateMessageHeight", () => {
  it("传 Session 时也复用内容投影，开合、跨 Session 和新消息版本仍改变估高", () => {
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
      useDisclosureStore.getState().clearSession(801);
      useDisclosureStore.getState().clearSession(802);
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

  it("用户消息：气泡按折行增高，胶囊按可读名称计入正文", () => {
    expect(estimateMessageHeight(user(text("问题")))).toBe(100);
    expect(estimateMessageHeight(user(text("请帮我看看。".repeat(20))))).toBe(146);
    // 文件、引用、Skill 是正文里的行内胶囊，不再有独立的附件区。
    expect(estimateMessageHeight(user(text('<file path="/tmp/a.md" />')))).toBe(100);
    expect(estimateMessageHeight(user(text('前文 <ref path="src/main.py" lines="12-14" /> 后文')))).toBe(100);
    expect(estimateMessageHeight(user(image))).toBe(149);
  });

  // 目的：长用户消息默认收起，估计按收起高度计；潜在缺陷：按全文估计时长消息滚进视口会被修正数百像素，页面跳动。
  it("用户消息：正文超过收起高度时按开合封顶，开合实时读取", () => {
    const long = user(text(Array.from({ length: 30 }, (_, line) => `第${line}行`).join("\n")));
    // 53 行外框 + 24 气泡内边距 + 240 收起高度 + 28「显示全部」一行；展开为 30 行 × 23。
    expect(estimateMessageHeight(long, 9)).toBe(53 + 24 + 240 + 28);
    useDisclosureStore.getState().set(9, "user:u", true);
    expect(estimateMessageHeight(long, 9)).toBe(53 + 24 + 30 * 23 + 28);
    useDisclosureStore.getState().clearSession(9);
    expect(estimateMessageHeight(long, 9)).toBe(53 + 24 + 240 + 28);
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
    expect(base).toBe(104);
    expect(estimateMessageHeight(assistant(text("# 标题\n\n一段话")))).toBe(152);
    expect(estimateMessageHeight(assistant(text("### 标题\n\n一段话")))).toBe(144);
    expect(estimateMessageHeight(assistant(text("- 一\n- 二\n- 三")))).toBe(184);
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
    useDisclosureStore.getState().clearSession(8);
  });

  it("compresses completed and active calls while budgeting all progress prose", () => {
    const calls = Array.from({ length: 20 }, (_, i) => call(`c${i}`));
    expect(estimateMessageHeight(running(...calls))).toBe(estimateMessageHeight(running(call("c"))));
    expect(estimateMessageHeight(running(...calls, call("live", "running")))).toBe(estimateMessageHeight(running(...calls)));
    expect(estimateMessageHeight(running(text("进展".repeat(100)), ...calls))).toBeGreaterThan(estimateMessageHeight(running(...calls)) + 100);
  });

  it("artifact and decision heights are independent of folded history", () => {
    // 等你操作的独立调用就地画成卡片：比同一位置的一行调用高。
    const settled = assistant(call("c", "completed", { presentation: "standalone" }));
    const waiting = assistant(call("c", "pending", { presentation: "standalone", awaitingUser: true }));
    expect(estimateMessageHeight(waiting)).toBeGreaterThan(estimateMessageHeight(settled) + 60);
    const changes = (paths: string[]) => assistant(call("w", "completed", { display_data: { kind: "file-changes", changes: paths.map((path) => ({ path, status: "modified" })) } }));
    expect(estimateMessageHeight(changes(["a", "b", "c"])) - estimateMessageHeight(changes(["a"]))).toBe(56);
  });

  // 目的：交付卡按胶囊行与说明行估高；潜在缺陷：一枚胶囊按旧的整行列表估高、说明不计高，滚动时跳动。
  it("交付卡按胶囊行估高，同一行再多几枚不变高，是独立的块、与改动卡无关，说明按行加高", () => {
    const present = (count: number) => call("p", "completed", { toolName: "present_deliverables", presentation: "quiet", deliverables: Array.from({ length: count }, (_, i) => deliveredFile(`f${i}.md`)) });
    const base = estimateMessageHeight(assistant(text("完")));
    expect(estimateMessageHeight(assistant(text("完"), present(1))) - base).toBe(36 + 16);
    expect(estimateMessageHeight(assistant(text("完"), present(3)))).toBe(estimateMessageHeight(assistant(text("完"), present(1))));
    expect(estimateMessageHeight(assistant(text("完"), present(4))) - estimateMessageHeight(assistant(text("完"), present(3)))).toBe(44);
    const change = call("w", "completed", { display_data: { kind: "file-changes", changes: [{ path: "a", status: "added" }] } });
    expect(estimateMessageHeight(assistant(text("完"), change, present(1))) - estimateMessageHeight(assistant(text("完"), change))).toBe(36 + 16);
    const noted = (note: string) => ({ ...present(1), display_data: { kind: "deliverables", note } });
    // 说明一行 24、与胶囊之间 gap-2；长说明按每行约 52 个汉字折行。
    expect(estimateMessageHeight(assistant(text("完"), noted("报告"))) - estimateMessageHeight(assistant(text("完"), present(1)))).toBe(24 + 8);
    expect(estimateMessageHeight(assistant(text("完"), noted("长".repeat(60)))) - estimateMessageHeight(assistant(text("完"), noted("报告")))).toBe(24);
  });

  // 潜在缺陷：图片方块与单张大图卡按胶囊高度估，滚动时跳动。
  it("图片方块按方块行估高，单张大图卡按记录里的宽高估，方块区与胶囊区之间加间距", () => {
    const present = (...deliverables: ReturnType<typeof deliveredFile>[]) => call("p", "completed", { toolName: "present_deliverables", presentation: "quiet", deliverables });
    const images = (count: number, extra: ReturnType<typeof deliveredFile>[] = []) => present(...Array.from({ length: count }, (_, i) => deliveredFile(`i${i}.png`, { kind: "image", width: 800, height: 400 })), ...extra);
    const base = estimateMessageHeight(assistant(text("完")));
    expect(estimateMessageHeight(assistant(text("完"), images(1))) - base).toBe(180 + 16);
    // 单张 SVG 没有宽高，渲染是方块，估高也按方块。
    expect(estimateMessageHeight(assistant(text("完"), present(deliveredFile("a.svg", { kind: "svg" })))) - base).toBe(96 + 16);
    expect(estimateMessageHeight(assistant(text("完"), images(2))) - base).toBe(96 + 16);
    expect(estimateMessageHeight(assistant(text("完"), images(2, [deliveredFile("a.md")]))) - base).toBe(96 + 8 + 36 + 16);
  });

  it("long Agent input starts compact and only grows on explicit disclosure", () => {
    const msg = { ...user(text("内容".repeat(1000))), metadata: { custom: { source: { sessionId: 2, name: "审查" } } } } as unknown as ThreadMessage;
    expect(estimateMessageHeight(msg, 8)).toBeLessThan(100);
    useDisclosureStore.getState().set(8, "agent:u", true);
    expect(estimateMessageHeight(msg, 8)).toBeGreaterThan(500);
    useDisclosureStore.getState().clearSession(8);
  });

});

describe("message height cache", () => {
  afterEach(() => forgetMessageHeights([1, 2]));

  it("按 Session 与列宽记住实测行高，列宽变了整桶作废", () => {
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

  it("没有记录时直接返回，不比较内容", () => {
    const stringify = vi.spyOn(JSON, "stringify");
    expect(rememberedMessageHeight(1, 720, "unmeasured", assistant(text("正文")))).toBeUndefined();
    expect(stringify).not.toHaveBeenCalled();
    stringify.mockRestore();
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
    useDisclosureStore.getState().clearSession(1);
  });

  it("Session 删除后丢弃其行高", () => {
    rememberMessageHeight(1, 720, "m1", 640);
    forgetMessageHeights([1]);
    expect(rememberedMessageHeight(1, 720, "m1")).toBeUndefined();
  });
});
