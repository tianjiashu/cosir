import { describe, expect, it } from "vitest";
import type { TransportMessagePart, TransportToolCallPart } from "@/lib/assistant/contract";
import { aggregateChanges, describeCallProblems, describeVerbGroups, projectTurn, visibleTurnBlock, type ActivityGroup, type TurnLayout } from "@/lib/assistant/turn/turn-layout";

/** 第一个工具组的摘要：失败、取消与按动词计数只在组上汇总。 */
const firstGroupSummary = (layout: TurnLayout) => (layout.blocks.find((block) => block.kind === "activity") as ActivityGroup).summary;
import { agentMessagePart, deliveredFile, outcome, toolCallPart } from "./transport-fixtures";
const text = (value: string, finalReply = false): TransportMessagePart => ({ type: "text", text: value, finalReply });
const call = (id: string, overrides: Partial<TransportToolCallPart> = {}) => toolCallPart(id, "read_file", { status: "completed", ...overrides });

describe("stable conversation projection", () => {
  it("途中来信切开工具组、随过程折叠，只有来信和最终回复时标题仍能展开", () => {
    const parts = [call("a"), agentMessagePart("5"), call("b"), text("结论", true)];
    const layout = projectTurn(parts, "settled", outcome("completed"));
    expect(layout.blocks.map((block) => block.kind)).toEqual(["activity", "agent", "activity", "narration"]);
    const letter = layout.blocks[1];
    expect(visibleTurnBlock(letter, false)).toBe(false);
    expect(visibleTurnBlock(letter, true)).toBe(true);
    expect(projectTurn([agentMessagePart("5"), text("结论", true)], "settled", outcome("completed")).process.items).toHaveLength(1);
    expect(projectTurn([agentMessagePart("5")], "running").intro).toBeNull();
  });

  it("only the active pre-output reply has compact chrome; whitespace and reasoning stay distinct from tools", () => {
    expect(projectTurn([], "running").intro).toBe("waiting");
    expect(projectTurn([text(" \n")], "running").blocks).toEqual([]);
    expect(projectTurn([text("答")], "running").intro).toBeNull();
    expect(projectTurn([call("first")], "running").intro).toBeNull();
    expect(projectTurn([call("wait", { toolName: "wait_messages", presentation: "quiet" })], "running").intro).toBeNull();
    for (const phase of ["settled", "suspended"] as const) expect(projectTurn([], phase).intro).toBeNull();
    for (const status of ["cancelled", "failed", "completed"] as const) expect(projectTurn([], "running", outcome(status)).intro).toBeNull();
  });
  it("appending a tool leaves existing prose in the same ordered slot", () => {
    const parts = [text("先读取"), call("one"), text("继续核对")];
    const before = projectTurn(parts, "running");
    const after = projectTurn([...parts, call("two", { status: "running" })], "running");
    expect(after.blocks.slice(0, 3)).toEqual(before.blocks);
    expect(after.answer.map((item) => item.part.text)).toEqual(["先读取", "继续核对"]);
    expect(after.defaultProcessOpen).toBe(true);
  });
  // 目的：Run 有结局前正文未定性，不随过程开合；潜在缺陷：运行中收起过程会把可能就是最终回复的正文一起藏起来。
  it("prose joins the process only once the Run has an outcome", () => {
    const parts = [text("先读取"), call("one"), text("回复中")];
    const running = projectTurn(parts, "running");
    expect(running.process.items.map((item) => item.kind)).toEqual(["call"]);
    for (const block of running.blocks) if (block.kind === "narration") expect(visibleTurnBlock(block, false)).toBe(true);
    const cancelled = projectTurn(parts, "settled", outcome("cancelled"));
    expect(cancelled.process.items.map((item) => item.kind)).toEqual(["narration", "call", "narration"]);
    for (const block of cancelled.blocks) if (block.kind === "narration") expect(visibleTurnBlock(block, false)).toBe(false);
  });
  it("only an explicit completed outcome folds the earlier prose; empty tail never hides all text", () => {
    const parts = [text("进展"), call("one"), text("回复", true)];
    expect(projectTurn(parts, "settled").hasFinalReply).toBe(false);
    expect(projectTurn(parts, "settled", outcome("cancelled")).defaultProcessOpen).toBe(true);
    expect(projectTurn(parts, "settled", outcome("failed")).answer).toHaveLength(2);
    const complete = projectTurn(parts, "settled", outcome("completed"));
    expect(complete.hasFinalReply).toBe(true);
    expect(complete.defaultProcessOpen).toBe(false);
    expect(complete.answer.map((item) => item.part.text)).toEqual(["回复"]);
    expect(projectTurn([text("进展"), call("one")], "settled", outcome("completed")).defaultProcessOpen).toBe(true);
  });
  it("wait stays out of work counts, but failures remain visible without renaming the interval", () => {
    const wait = call("wait", { toolName: "wait_messages", presentation: "quiet" });
    const before = projectTurn([wait, call("read")], "suspended");
    const after = projectTurn([{ ...wait, status: "failed" }, call("read")], "suspended");
    expect(before.blocks[0]).toMatchObject({ key: "interval:0", calls: [{ part: { toolCallId: "read" } }] });
    expect(after.blocks[0]).toMatchObject({ key: "interval:0" });
    expect(firstGroupSummary(after).verbs.map(({ label, count }) => ({ label, count }))).toEqual([{ label: "读取文件", count: 1 }]);
    expect(firstGroupSummary(after).failedCount).toBe(1);
    expect(projectTurn([wait], "suspended").process.items).toEqual([]);
  });
  it("body and standalone calls delimit tool groups; a call awaiting the user stays in its own slot", () => {
    const pending = call("decision", { awaitingUser: true, status: "pending", presentation: "standalone" });
    const parts = [call("a"), text("说明"), pending, call("b")];
    const layout = projectTurn(parts, "suspended");
    expect(layout.blocks.map((item) => item.kind)).toEqual(["activity", "narration", "standalone", "activity"]);
    expect(layout.blocks[2]).toMatchObject({ index: 2, part: pending });
  });
  // 目的：模型常在调用前吐一个换行；潜在缺陷：不可见的空白正文把连续调用切成两个工具组，计数与当前活动跟着跳。
  it("whitespace-only prose neither renders nor splits a tool interval", () => {
    const layout = projectTurn([call("a"), text("\n"), call("b")], "running");
    expect(layout.blocks.map((item) => item.kind)).toEqual(["activity"]);
    expect((layout.blocks[0] as ActivityGroup).calls.map((item) => item.index)).toEqual([0, 2]);
  });
  // 目的：组头文案按首次出现排列；潜在缺陷：按次数排序时后出现的类别次数反超，已显示的类别来回换位。
  it("group heading lists verbs by first appearance, two at most", () => {
    const write = (id: string) => call(id, { toolName: "write_file" });
    const run = (id: string) => call(id, { toolName: "execute_terminal" });
    const heading = (parts: TransportMessagePart[]) => describeVerbGroups(firstGroupSummary(projectTurn(parts, "running")));
    expect(heading([write("w1"), write("w2"), write("w3"), run("r1"), run("r2")])).toBe("写入文件 3 次，执行命令 2 次");
    expect(heading([run("r1"), run("r2"), run("r3"), write("w1"), write("w2"), write("w3"), call("x"), call("y"), call("z")])).toBe("执行命令 3 次，写入文件 3 次 等 3 类");
  });
  // 目的：不展开也知道这段碰了什么；潜在缺陷：只写次数时用户读不出任何东西，次数多时列对象又成了噪音。
  it("names what one or two calls touched and falls back to counts", () => {
    const read = (id: string, path?: string) => call(id, { args: path ? { path } : {} });
    const run = (id: string, args: Record<string, unknown> = {}) => call(id, { toolName: "execute_terminal", args });
    const heading = (...parts: TransportMessagePart[]) => describeVerbGroups(firstGroupSummary(projectTurn(parts, "settled")));
    expect(heading(read("a", "src/app/auth.py"))).toBe("读取文件 auth.py");
    expect(heading(read("a", "src/auth.py"), read("b", "src/db.py"))).toBe("读取文件 auth.py、db.py");
    expect(heading(read("a", "src/auth.py"), read("b", "lib/auth.py"))).toBe("读取文件 auth.py");
    expect(heading(read("a", "a.py"), read("b", "b.py"), read("c", "c.py"))).toBe("读取文件 3 次");
    // 写了意图就写意图（自带动作），没写的命令原文难读，只写动词或次数。
    expect(heading(run("r", { intent: "查看配置项", command: "cat .env" }), read("a", "auth.py"))).toBe("查看配置项，读取文件 auth.py");
    expect(heading(run("r", { command: "cat .env" }))).toBe("执行命令");
    expect(heading(run("r1", { intent: "查看配置项" }), run("r2", { command: "ls" }))).toBe("执行命令 2 次");
  });
  it("completed result warnings are counted per call, not per failed item", () => {
    const layout = projectTurn([
      call("terminal", { display_data: { kind: "terminal-result", exit_code: 1 } }),
      call("images", { display_data: { kind: "image-read", failures: ["a", "b"] } }),
      call("browser", { display_data: { kind: "browser-page", steps: [{ status: "skipped" }] } }),
      call("web", { display_data: { kind: "web-fetch-urls", partial: true } }),
    ], "settled");
    expect(firstGroupSummary(layout).failedCount).toBe(0);
    expect(describeCallProblems(firstGroupSummary(layout))).toBeNull();
    expect(firstGroupSummary(layout).verbs.map(({ label, count }) => ({ label, count }))).toEqual([{ label: "读取文件", count: 4 }]);
  });
  it("unknown tools remain visible and do not acquire invented result semantics", () => {
    const layout = projectTurn([call("future", { toolName: "future_tool", status: "completed", display_data: { kind: "future-result" } })], "settled");
    expect(describeVerbGroups(firstGroupSummary(layout))).toBe("future_tool");
  });
  it("keeps every file operation in order, including repeated edits of one path", () => {
    const change = (id: string, status: string, insertions: number) => call(id, { display_data: { kind: "file-changes", changes: [{ path: "a", status, insertions }] } });
    const card = aggregateChanges([{ index: 0, part: change("edit", "modified", 3) }, { index: 1, part: change("new", "added", 12) }]);
    expect(card.totals).toEqual({ files: 1, calls: 2 });
    expect(card.files[0].operations.map((op) => [op.toolCallId, op.change.status, op.change.insertions])).toEqual([["edit", "modified", 3], ["new", "added", 12]]);
  });
  it("keeps artifacts and required decisions available independently of process folding", () => {
    const question = call("question", { toolName: "ask_user", presentation: "standalone", awaitingUser: true, status: "pending", display_data: { kind: "user-question" } });
    const draft = call("draft", { display_data: { kind: "agent-configuration-draft" } });
    const layout = projectTurn([question, draft, text("回复", true)], "settled", outcome("completed"));
    expect(layout.defaultProcessOpen).toBe(false);
    // 过程收起时，等你操作的调用仍在原位置常显；决定之后随过程开合。
    expect(visibleTurnBlock(layout.blocks[0], false)).toBe(true);
    expect(visibleTurnBlock({ ...layout.blocks[0], part: { ...question, awaitingUser: false } } as typeof layout.blocks[0], false)).toBe(false);
    expect(layout.artifacts.map((item) => item.kind)).toEqual(["agent-configuration-draft"]);
  });
});

it("reuses unchanged tool groups and file aggregation across prose versions, but refreshes changed calls", () => {
  const change = toolCallPart("write", "write_file", { status: "completed", display_data: { kind: "file-changes", changes: [{ path: "a.ts", status: "modified", insertions: 2 }] } });
  const first = projectTurn([change, { type: "text", text: "a" }], "running");
  const streamed = projectTurn([change, { type: "text", text: "ab" }], "running", null, first);
  expect(streamed.blocks[0]).toBe(first.blocks[0]);
  expect(streamed.artifacts[0]).toBe(first.artifacts[0]);
  expect(firstGroupSummary(streamed)).toBe(firstGroupSummary(first));
  const failed = projectTurn([{ ...change, status: "failed" }, { type: "text", text: "ab" }], "settled", null, streamed);
  expect(failed.blocks[0]).not.toBe(first.blocks[0]);
  expect(firstGroupSummary(failed).failedCount).toBe(1);
  const updated = projectTurn([{ ...change, display_data: { kind: "file-changes", changes: [{ path: "b.ts", status: "added" }] } }], "settled", null, failed);
  expect(updated.artifacts[0]).toMatchObject({ files: [{ path: "b.ts" }] });
});

describe("回复呈现工具", () => {
  const present = (id: string, paths: string[]) => call(id, { toolName: "present_deliverables", presentation: "quiet", deliverables: paths.map((path) => deliveredFile(path)) });
  const files = present("files", ["a.md"]);
  const offer = (id: string, ask: string, overrides: Partial<TransportToolCallPart> = {}) =>
    call(id, { toolName: "offer_next_step", presentation: "quiet", display_data: { kind: "next-step", ask, replies: ["好"], recap: "做完了。" }, ...overrides });

  // 目的：卡片就是这两个工具的呈现；潜在缺陷：静默调用又在过程里多出一行，或交付卡随过程折叠消失。
  it("交付卡是回合里的块、过程收起也常显；建议单独给出；两者都不在过程里留调用行", () => {
    const layout = projectTurn([files, offer("o1", "继续吗？"), text("做完了", true)], "settled", outcome("completed"));
    expect(layout.blocks.map((block) => block.kind)).toEqual(["deliverables", "activity", "narration"]);
    expect(visibleTurnBlock(layout.blocks[0], false)).toBe(true);
    expect(layout.artifacts).toEqual([]);
    expect(layout.nextStep?.part.toolCallId).toBe("o1");
    expect(layout.process.items).toHaveLength(0);
  });

  // 目的：一次交付一张卡、放在调用的位置，运行中就出现；潜在缺陷：说明对不上那批成果、卡片要等落定才出现，或流式追加正文时卡片重建导致胶囊重挂载。
  it("每次交付一张卡、在调用的位置，运行中就在，正文流式追加时保持同一引用", () => {
    const change = call("w", { display_data: { kind: "file-changes", changes: [{ path: "a.md", status: "added" }] } });
    const parts = [change, present("p1", ["a.md"]), call("r", {}), present("p2", ["b.md", "a.md"]), text("a")];
    const kinds = (layout: ReturnType<typeof projectTurn>) => layout.blocks.map((block) => block.kind === "deliverables" ? block.part.toolCallId : block.kind);
    const running = projectTurn(parts, "running");
    // p1 的 a.md 后来又交付了一遍，只留在 p2 里，p1 整张不出现、留在活动段里静默；交付卡像旁白一样切断活动段。
    expect(kinds(running)).toEqual(["activity", "p2", "narration"]);
    const first = projectTurn(parts, "settled", outcome("completed"), running);
    const card = first.blocks.find((block) => block.kind === "deliverables")!;
    expect(card.kind === "deliverables" && card.items.map((item) => item.deliverable.key)).toEqual(["/w/b.md", "/w/a.md"]);
    expect(first.artifacts.map((item) => item.kind)).toEqual(["changes"]);
    const streamed = projectTurn([...parts.slice(0, -1), text("ab")], "settled", outcome("completed"), first);
    expect(streamed.blocks.find((block) => block.kind === "deliverables")).toBe(card);
  });

  // 目的：交付卡在最终回复之上；潜在缺陷：模型把答案和交付写进同一条回复时，卡片夹在答案与收尾之间。
  it("落定后排在最终回复之后的交付卡提到第一段最终回复之前，运行中不动", () => {
    const parts = [call("r", {}), text("完整答案"), present("p", ["a.md"]), text("收尾")];
    const kinds = (layout: ReturnType<typeof projectTurn>) => layout.blocks.map((block) => block.kind);
    expect(kinds(projectTurn(parts, "running"))).toEqual(["activity", "narration", "deliverables", "narration"]);
    const settled = [parts[0], { ...text("完整答案"), finalReply: true }, parts[2], { ...text("收尾"), finalReply: true }] as TransportMessagePart[];
    expect(kinds(projectTurn(settled, "settled", outcome("completed")))).toEqual(["activity", "deliverables", "narration", "narration"]);
  });

  it("同一回合多次建议只认最后一次，失败的调用没有展示数据、不算", () => {
    const failed = offer("o3", "", { status: "failed", display_data: null });
    const layout = projectTurn([offer("o1", "先问"), offer("o2", "后问"), failed, text("完", true)], "settled", outcome("completed"));
    expect(layout.nextStep?.part.toolCallId).toBe("o2");
    // 失败的静默调用照常露出，让人看得到出了问题。
    expect(layout.process.items).toHaveLength(1);
    expect(projectTurn([text("完", true)], "settled", outcome("completed")).nextStep).toBeNull();
    // 展示数据形状不对（缺回顾）时不算建议，卡片与痕迹都不画。
    const malformed = offer("o4", "问", { display_data: { kind: "next-step", ask: "问", replies: ["好"] } });
    expect(projectTurn([malformed, text("完", true)], "settled", outcome("completed")).nextStep).toBeNull();
  });

  it("正文流式追加时建议对象保持同一引用", () => {
    const part = offer("o1", "继续吗？");
    const first = projectTurn([part, text("a")], "running");
    expect(projectTurn([part, text("ab")], "running", null, first).nextStep).toBe(first.nextStep);
  });
});

describe("活动段汇总的产出", () => {
  const SHA = (c: string) => c.repeat(64);
  const edit = (id: string, changes: { path: string; insertions: number; deletions: number }[]) => toolCallPart(id, "edit_file", {
    status: "completed", display_data: { kind: "file-changes", changes: changes.map((change) => ({ ...change, status: "modified", patch: null })) },
  });
  const read = (id: string, assets: string[]) => toolCallPart(id, "read_image", {
    status: "completed", display_data: { kind: "image-read", images: assets.map((asset, i) => ({ path: `img/${id}-${i}.png`, asset_id: asset, width: 1, height: 1 })), failures: [] },
  });
  const shot = (id: string, asset: string) => toolCallPart(id, "browser_screenshot", {
    status: "completed", display_data: { kind: "browser-page", tab_id: "t1", url: "https://example.com", title: "示例", screenshot: asset },
  });
  const group = (parts: TransportMessagePart[], previous?: TurnLayout) => {
    const layout = projectTurn(parts, "running", null, previous ?? null);
    return { layout, group: layout.blocks.find((block): block is ActivityGroup => block.kind === "activity")! };
  };

  // 目的：汇总行的「改了多少、看到了什么」与调用行同源；潜在缺陷：只数第一个文件、图片顺序乱、失败调用冒出图片。
  it("sums every file operation and lists images in the order they appeared", () => {
    const failed = toolCallPart("bad", "read_image", { status: "failed", display_data: null });
    const mcp = toolCallPart("m1", "mcp__figma__render", { status: "completed", display_data: { kind: "mcp-tool-result", server_name: "figma", tool_name: "render", scope: "system", images: [SHA("d")] } });
    const { group: g } = group([
      edit("e1", [{ path: "a.ts", insertions: 3, deletions: 1 }, { path: "b.ts", insertions: 2, deletions: 0 }]),
      read("r1", [SHA("a"), SHA("b")]), failed, shot("s1", SHA("c")), mcp,
      edit("e2", [{ path: "a.ts", insertions: 1, deletions: 4 }]),
    ]);
    expect(g.summary.diff).toEqual({ insertions: 6, deletions: 5 });
    expect(g.summary.images).toEqual([
      { assetId: SHA("a"), alt: "img/r1-0.png" }, { assetId: SHA("b"), alt: "img/r1-1.png" }, { assetId: SHA("c"), alt: "示例" },
      { assetId: SHA("d"), alt: "figma / render" },
    ]);
  });

  // 目的：运行中新的调用到达时汇总跟着累加，没变时沿用旧对象（行不重渲染）。
  it("grows with new calls and is reused while calls stay the same", () => {
    const first = [read("r1", [SHA("a")])];
    const one = group(first);
    expect(group(first, one.layout).group).toBe(one.group);
    const two = group([...first, shot("s1", SHA("c"))], one.layout);
    expect(two.group.summary.images.map((image) => image.assetId)).toEqual([SHA("a"), SHA("c")]);
    expect(two.group.summary.diff).toEqual({ insertions: 0, deletions: 0 });
  });
});
