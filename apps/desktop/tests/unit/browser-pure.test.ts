import { describe, expect, it } from "vitest";

import { HostMethodError } from "@/electron/main/host-link/methods";
import { parseDropTasks, parseNavigate, parseSnapshot } from "@/electron/main/browser/params";
import { formatSnapshot, type AXNode } from "@/electron/main/browser/snapshot-format";
import { TabTable } from "@/electron/main/browser/tab-table";

const LIMITS = { perTask: 2, total: 3 };

function codeOf(run: () => unknown): string {
  try {
    run();
  } catch (error) {
    if (error instanceof HostMethodError) return error.code;
    throw error;
  }
  return "no error";
}

describe("TabTable", () => {
  // 目的：上限两层都生效且消息可区分；潜在缺陷：子 Agent 一多就把宿主内存吃满。
  it("enforces the per-task and the global limit separately", () => {
    const table = new TabTable();
    table.add(1);
    table.add(1);
    expect(() => table.assertCanOpen(1, LIMITS)).toThrow(/per-task/);
    table.add(2);
    expect(() => table.assertCanOpen(3, LIMITS)).toThrow(/global/);
    expect(codeOf(() => table.assertCanOpen(3, LIMITS))).toBe("TAB_LIMIT");
  });

  // 目的：标签归属是硬边界；潜在缺陷：子 Agent 读到或操作父 Agent 的页面。
  it("hides other tasks' tabs behind TAB_NOT_FOUND", () => {
    const table = new TabTable();
    const tab = table.add(1);
    expect(table.require(1)).toBe(tab);
    expect(table.require(1, tab)).toBe(tab);
    expect(codeOf(() => table.require(2, tab))).toBe("TAB_NOT_FOUND");
    expect(codeOf(() => table.require(2))).toBe("TAB_NOT_FOUND");
  });

  it("falls back to the newest remaining tab when the current one closes, never reusing ids", () => {
    const table = new TabTable();
    const first = table.add(1);
    const second = table.add(1);
    const third = table.add(1);
    table.select(1, first);
    table.remove(first);
    expect(table.require(1)).toBe(third);
    table.remove(third);
    table.remove(second);
    expect(table.hasCurrent(1)).toBe(false);
    expect(table.add(1)).toBe("t4");
  });
});

describe("params", () => {
  const base = { task_id: 7, limits: { per_task: 5, total: 20 } };

  it("accepts a url or a history action", () => {
    expect(parseNavigate({ ...base, url: "https://example.com/a" }).target).toEqual({ url: "https://example.com/a" });
    expect(parseNavigate({ ...base, action: "back", tab: "t3" })).toMatchObject({ tab: "t3", target: { action: "back" } });
    expect(parseSnapshot({ task_id: 7, tab: null })).toEqual({ taskId: 7, tab: undefined });
    expect(parseDropTasks({ task_ids: [1, 2] })).toEqual([1, 2]);
  });

  // 目的：Agent 只能打开网页；潜在缺陷：经浏览器读本机文件或执行 javascript: 地址。
  it("blocks non-http schemes and rejects malformed params", () => {
    expect(codeOf(() => parseNavigate({ ...base, url: "file:///etc/passwd" }))).toBe("NAVIGATION_BLOCKED");
    expect(codeOf(() => parseNavigate({ ...base, url: "javascript:alert(1)" }))).toBe("NAVIGATION_BLOCKED");
    expect(codeOf(() => parseNavigate({ ...base, url: "not a url" }))).toBe("INVALID_PARAMS");
    expect(codeOf(() => parseNavigate({ ...base, action: "home" }))).toBe("INVALID_PARAMS");
    expect(codeOf(() => parseNavigate({ url: "https://a.b", limits: base.limits }))).toBe("INVALID_PARAMS");
    expect(codeOf(() => parseNavigate({ ...base, url: "https://a.b", tab: "../t1" }))).toBe("INVALID_PARAMS");
    expect(codeOf(() => parseNavigate({ task_id: 7, url: "https://a.b", limits: { per_task: 0, total: 1 } }))).toBe("INVALID_PARAMS");
  });
});

function node(nodeId: string, role: string, extra: Partial<AXNode> = {}): AXNode {
  return { nodeId, role: { value: role }, ...extra };
}

describe("formatSnapshot", () => {
  const tree: AXNode[] = [
    node("1", "RootWebArea", { name: { value: "Shop" }, childIds: ["2"] }),
    node("2", "generic", { parentId: "1", childIds: ["3", "4", "5", "6", "9", "10"] }),
    node("3", "heading", { parentId: "2", name: { value: "Welcome" }, properties: [{ name: "level", value: { value: 1 } }], childIds: ["3a"] }),
    node("3a", "StaticText", { parentId: "3", name: { value: "Welcome" } }),
    node("4", "StaticText", { parentId: "2", name: { value: "Hello" } }),
    node("5", "StaticText", { parentId: "2", name: { value: "  there " } }),
    node("6", "textbox", { parentId: "2", name: { value: "Email" }, value: { value: "a@b.c" }, backendDOMNodeId: 61, properties: [{ name: "required", value: { value: true } }] }),
    node("9", "button", { parentId: "2", name: { value: "Buy" }, backendDOMNodeId: 91, properties: [{ name: "disabled", value: { value: true } }] }),
    node("10", "Iframe", { parentId: "2" }),
  ];

  it("gives refs only to interactive nodes and folds noise", () => {
    const snapshot = formatSnapshot(tree);
    expect(snapshot.text).toBe([
      "- heading \"Welcome\" [level=1]",
      "  - text \"Welcome\"",
      "- text \"Hello there\"",
      "- textbox \"Email\" [ref=e1] [required] value=\"a@b.c\"",
      "- button \"Buy\" [ref=e2] [disabled]",
      "- iframe (contents not shown)",
    ].join("\n"));
    expect([...snapshot.refs]).toEqual([["e1", 61], ["e2", 91]]);
    expect(snapshot.frames).toBe(1);
  });

  // 目的：快照自己守预算；潜在缺陷：后端输出预算把快照切在半行、模型看不出还有内容。
  it("cuts at whole lines and reports what was left out", () => {
    const text = formatSnapshot(tree, 60).text;
    expect(text.split("\n").at(-1)).toMatch(/^… \d+ more lines not shown$/);
    expect(text.length).toBeLessThan(120);
  });

  it("describes an empty page", () => {
    expect(formatSnapshot([node("1", "RootWebArea")]).text).toBe("(the page has no readable content)");
  });
});
