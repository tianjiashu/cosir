import { describe, expect, it } from "vitest";

import type { TransportMessagePart, TransportToolCallPart } from "@/lib/assistant/contract";
import { ACTIVE_TAIL_SIZE, aggregateChanges, callTitle, projectTurn } from "@/lib/assistant/turn-layout";
import { toolCallPart } from "./transport-fixtures";

const text = (value: string): TransportMessagePart => ({ type: "text", text: value });
const reasoning = (value: string): TransportMessagePart => ({ type: "reasoning", text: value });
const call = (id: string, overrides: Partial<TransportToolCallPart> = {}): TransportToolCallPart => toolCallPart(id, "read_file", {
  status: "completed",
  presentation: { verb: "读取文件", icon: "eye" },
  heading: { title: "读取文件", target: `${id}.ts` },
  ...overrides,
});
const changes = (id: string, files: Array<Record<string, unknown>>) => call(id, {
  toolName: "apply_patch",
  presentation: { verb: "修改文件", icon: "git-compare" },
  heading: { title: "修改文件", target: null },
  display_data: { kind: "file-changes", changes: files },
});

const indexes = (items: ReadonlyArray<{ index: number }>) => items.map((item) => item.index);

describe("projectTurn 分区", () => {
  // 目的：交替序列下过程只按原序排、结论只取最后一个非正文 part 之后的正文；潜在缺陷：中间叙述被当成结论。
  it("思考 / 调用 / 叙述交替：结论是最后一个非正文 part 之后的正文，其余按原序进过程", () => {
    const parts = [
      reasoning("先看结构"), call("c1"), text("看完目录，接着搜实现。"), reasoning("再搜"),
      call("c2", { presentation: { verb: "搜索内容", icon: "search" }, heading: { title: "搜索内容", target: "foo" } }), call("c3"), text("结论第一段"), text("结论第二段"),
    ];
    const layout = projectTurn(parts, "settled");
    expect(layout.answer.map((item) => item.part.text)).toEqual(["结论第一段", "结论第二段"]);
    expect(indexes(layout.answer)).toEqual([6, 7]);
    expect(layout.process.items.map((item) => `${item.kind}:${item.index}`)).toEqual([
      "reasoning:0", "call:1", "narration:2", "reasoning:3", "call:4", "call:5",
    ]);
    expect(layout.process.callCount).toBe(3);
    expect(layout.process.verbCounts).toEqual([{ label: "读取文件", count: 2 }, { label: "搜索内容", count: 1 }]);
    expect(layout.process.flat).toBe(false);
    expect(layout.process.currentAction).toBeNull();
    expect(layout.process.tail).toBe(layout.process.items);
    expect(layout.process.earlierCount).toBe(0);
  });

  // 目的：流式期间末尾正文暂时算结论，不发生跳动；回合结束那一次才定稿。
  it("运行中的末尾正文先算结论，之后跟出调用时它退回叙述", () => {
    const streaming = projectTurn([call("c1", { status: "completed" }), text("我先看一下")], "running");
    expect(indexes(streaming.answer)).toEqual([1]);
    expect(streaming.process.items.map((item) => item.kind)).toEqual(["call"]);
    expect(streaming.process.currentAction).toBe("思考中");

    const continued = projectTurn([call("c1"), text("我先看一下"), call("c2", { status: "running" })], "running");
    expect(continued.answer).toEqual([]);
    expect(continued.process.items.map((item) => item.kind)).toEqual(["call", "narration", "call"]);
    expect(continued.process.currentAction).toBe("读取文件");
  });

  it("运行中途没有结论：整段都是过程，当前动作取最新未结束的调用", () => {
    const layout = projectTurn([
      reasoning("想"), call("c1", { status: "completed" }),
      call("c2", { status: "running", heading: { title: "执行命令", target: "npm test" } }),
      call("c3", { status: "pending", heading: { title: "读取文件", target: "b.ts" } }),
    ], "running");
    expect(layout.answer).toEqual([]);
    // 最新的未结束调用是排队中的 c3，当前动作取它的标题。
    expect(layout.process.currentAction).toBe("读取文件");
    expect(layout.process.callCount).toBe(3);
  });

  it("只有思考：没有调用、没有结论，过程平铺一行思考", () => {
    const layout = projectTurn([reasoning("在想")], "running");
    expect(layout.process.items).toEqual([{ kind: "reasoning", index: 0, part: reasoning("在想") }]);
    expect(layout.process.callCount).toBe(0);
    expect(layout.process.flat).toBe(true);
    expect(layout.process.verbCounts).toEqual([]);
    expect(layout.process.currentAction).toBe("思考中");
    expect(layout.answer).toEqual([]);
  });

  it("没有任何非正文 part 时整条消息都是结论", () => {
    const layout = projectTurn([text("直接回答"), text("补充")], "settled");
    expect(indexes(layout.answer)).toEqual([0, 1]);
    expect(layout.process.items).toEqual([]);
    expect(layout.process.flat).toBe(true);
  });

  it("只有一次调用时平铺，不加区头", () => {
    const layout = projectTurn([reasoning("看看"), call("c1"), text("答")], "settled");
    expect(layout.process.flat).toBe(true);
    expect(layout.process.callCount).toBe(1);
    expect(projectTurn([call("c1"), call("c2"), text("答")], "settled").process.flat).toBe(false);
  });

  it("标题回落：heading.title → presentation.verb → 工具名", () => {
    expect(callTitle(call("c1"))).toBe("读取文件");
    expect(callTitle(toolCallPart("c2", "web_search", { presentation: { verb: "搜索网页" } }))).toBe("搜索网页");
    expect(callTitle(toolCallPart("c3", "wait_messages"))).toBe("wait_messages");
    const layout = projectTurn([toolCallPart("c2", "web_search", { presentation: { verb: "搜索网页" } }), toolCallPart("c3", "wait_messages")], "settled");
    expect(layout.process.verbCounts).toEqual([{ label: "搜索网页", count: 1 }, { label: "wait_messages", count: 1 }]);
  });

  it("区头计数按动词聚合，不按各异的意图标题", () => {
    const run = (id: string, title: string) => toolCallPart(id, "execute_terminal", {
      presentation: { verb: "执行命令", icon: "terminal" },
      heading: { title, target: "bun test" },
    });
    const layout = projectTurn([run("c1", "安装依赖"), run("c2", "运行单元测试"), toolCallPart("c3", "read_file", { presentation: { verb: "读取文件" } })], "settled");
    expect(layout.process.verbCounts).toEqual([{ label: "执行命令", count: 2 }, { label: "读取文件", count: 1 }]);
    expect(callTitle(run("c1", "安装依赖"))).toBe("安装依赖");
  });
});

describe("projectTurn 等待用户、失败与活跃尾部", () => {
  it("awaitingUser 的调用提进等待区，同时留在过程里", () => {
    const handoff = toolCallPart("h1", "browser_handoff", { status: "pending", awaitingUser: true, heading: { title: "交给你登录", target: null } });
    const layout = projectTurn([call("c1"), handoff], "suspended");
    expect(layout.attention).toEqual([{ index: 1, part: handoff }]);
    expect(layout.process.items.map((item) => item.index)).toEqual([0, 1]);
    // pending 本身不是等待用户：排队待执行的调用不进等待区。
    expect(projectTurn([call("c2", { status: "pending" })], "running").attention).toEqual([]);
  });

  it("失败与取消分别计数", () => {
    const layout = projectTurn([
      call("c1", { status: "failed" }), call("c2", { status: "cancelled" }), call("c3", { status: "failed" }), call("c4"),
    ], "settled");
    expect(layout.process.failedCount).toBe(2);
    expect(layout.process.cancelledCount).toBe(1);
    expect(layout.process.callCount).toBe(4);
  });

  // 目的：长回合运行时页面高度有上界；潜在缺陷：结束后也只给尾部，展开过程时看不到完整回放。
  it("运行中只保留最近 4 个过程项，更早的折成 earlierCount；结束后给全部", () => {
    const parts = [reasoning("r0"), call("c1"), text("n1"), call("c2"), reasoning("r1"), call("c3", { status: "running" })];
    const running = projectTurn(parts, "running");
    expect(running.process.items).toHaveLength(6);
    expect(running.process.tail).toHaveLength(ACTIVE_TAIL_SIZE);
    expect(indexes(running.process.tail)).toEqual([2, 3, 4, 5]);
    expect(running.process.earlierCount).toBe(2);

    for (const phase of ["settled", "suspended"] as const) {
      const layout = projectTurn(parts, phase);
      expect(layout.process.tail).toHaveLength(6);
      expect(layout.process.earlierCount).toBe(0);
      expect(layout.process.currentAction).toBeNull();
    }
  });
});

describe("projectTurn 产物", () => {
  it("多个 file-changes 聚合成一张「本轮改动」卡，按 path 合并、状态取最终态、增删累加", () => {
    const parts = [
      changes("w1", [{ path: "src/a.ts", status: "added", insertions: 10, deletions: 0 }]),
      changes("w2", [
        { path: "src/a.ts", status: "modified", insertions: 3, deletions: 1 },
        { path: "src/b.ts", status: "modified", insertions: 2, deletions: 2 },
      ]),
      changes("w3", [{ path: "src/b.ts", status: "deleted", insertions: 0, deletions: 4 }]),
      changes("w4", [{ path: "src/c.ts", status: "moved", new_path: "src/d.ts", insertions: 0, deletions: 0 }]),
      text("改完了"),
    ];
    const layout = projectTurn(parts, "settled");
    expect(layout.artifacts).toHaveLength(1);
    const card = layout.artifacts[0];
    expect(card.kind).toBe("changes");
    if (card.kind !== "changes") throw new Error("unreachable");
    expect(card.files).toEqual([
      { path: "src/a.ts", newPath: null, status: "added", additions: 13, deletions: 1 },
      { path: "src/b.ts", newPath: null, status: "deleted", additions: 2, deletions: 6 },
      { path: "src/c.ts", newPath: "src/d.ts", status: "moved", additions: 0, deletions: 0 },
    ]);
    expect(card.totals).toEqual({ files: 3, additions: 15, deletions: 7 });
    expect(card.callIndexes).toEqual([0, 1, 2, 3]);
    // 改动行在过程里也有：产物卡与过程行是同一批调用的两种视角。
    expect(layout.process.callCount).toBe(4);
    expect(layout.process.verbCounts).toEqual([{ label: "修改文件", count: 4 }]);
  });

  it("最终状态优先级：deleted > moved > added > modified，低优先级不覆盖高优先级", () => {
    const card = aggregateChanges([
      { index: 0, part: changes("w1", [{ path: "x", status: "deleted" }]) },
      { index: 1, part: changes("w2", [{ path: "x", status: "modified", insertions: 1 }]) },
      { index: 2, part: changes("w3", [{ path: "y", status: "moved", new_path: "z" }]) },
      { index: 3, part: changes("w4", [{ path: "y", status: "added" }]) },
    ]);
    expect(card.files).toEqual([
      { path: "x", newPath: null, status: "deleted", additions: 1, deletions: 0 },
      { path: "y", newPath: "z", status: "moved", additions: 0, deletions: 0 },
    ]);
  });

  it("草稿与 Team 审批各自成卡，改动卡排在最前；未登记的 kind 不进产物区", () => {
    const draft = toolCallPart("d1", "propose_agent_configuration", { status: "completed", display_data: { kind: "agent-configuration-draft" } });
    const team = toolCallPart("t1", "agent_team", { status: "pending", awaitingUser: true, display_data: { kind: "agent-team-approval" } });
    const teamDraft = toolCallPart("t2", "propose_agent_team_configuration", { status: "completed", display_data: { kind: "agent-team-configuration-draft" } });
    const plain = toolCallPart("r1", "read_file", { status: "completed", display_data: { kind: "read-file-meta" } });
    const layout = projectTurn([draft, team, plain, teamDraft, changes("w1", [{ path: "a", status: "modified" }])], "settled");
    expect(layout.artifacts.map((card) => card.kind)).toEqual([
      "changes", "agent-configuration-draft", "agent-team-approval", "agent-team-configuration-draft",
    ]);
    expect(layout.artifacts.slice(1).map((card) => (card.kind === "changes" ? -1 : card.index))).toEqual([0, 1, 3]);
    expect(layout.attention.map((item) => item.index)).toEqual([1]);
  });

  it("运行中尚无展示数据的写入调用只在过程里，完成后才出现在产物区", () => {
    const running = toolCallPart("w1", "write_file", { status: "running", heading: { title: "写入文件", target: "a.ts" } });
    expect(projectTurn([running], "running").artifacts).toEqual([]);
    expect(projectTurn([changes("w1", [{ path: "a.ts", status: "added", insertions: 5 }])], "running").artifacts).toHaveLength(1);
  });
});
