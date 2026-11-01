import { describe, expect, it } from "vitest";

import { HostMethodError } from "@/electron/main/host-link/methods";
import { runActions, type PageSession } from "@/electron/main/browser/page-input";
import { parseAct, parseDropTasks, parseNavigate, parseSnapshot, type PageAction } from "@/electron/main/browser/params";
import { RefRegistry } from "@/electron/main/browser/ref-registry";
import { formatSnapshot, type AXNode } from "@/electron/main/browser/snapshot-format";


function codeOf(run: () => unknown): string {
  try {
    run();
  } catch (error) {
    if (error instanceof HostMethodError) return error.code;
    throw error;
  }
  return "no error";
}

describe("params", () => {
  const base = { task_id: 7, limits: { per_task: 5, total: 20 } };

  it("accepts a url or a history action", () => {
    expect(parseNavigate({ ...base, url: "https://example.com/a" }).target).toEqual({ url: "https://example.com/a" });
    expect(parseNavigate({ ...base, action: "back", tab: "t3" })).toMatchObject({ tab: "t3", target: { action: "back" } });
    expect(parseSnapshot({ task_id: 7, tab: null })).toEqual({ taskId: 7, tab: undefined, fromLine: 1 });
    expect(parseSnapshot({ task_id: 7, from_line: 40 })).toMatchObject({ fromLine: 40 });
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
    expect(codeOf(() => parseSnapshot({ task_id: 7, from_line: 0 }))).toBe("INVALID_PARAMS");
  });

  it("parses the action set, filling defaults the backend leaves out", () => {
    const { actions } = parseAct({ task_id: 7, actions: [
      { type: "hover", ref: "e3" },
      { type: "press", key: "k", modifiers: ["ControlOrMeta"], ref: "e4" },
      { type: "press", key: "Enter" },
      { type: "scroll", direction: "down", ref: "e5" },
      { type: "scroll", ref: "e5" },
      { type: "wait_for", text_gone: "Loading" },
    ] });
    expect(actions).toEqual([
      { type: "hover", ref: "e3" },
      { type: "press", key: "k", modifiers: ["ControlOrMeta"], ref: "e4" },
      { type: "press", key: "Enter", modifiers: [] },
      { type: "scroll", direction: "down", ref: "e5" },
      { type: "scroll", ref: "e5" },
      { type: "wait_for", text: undefined, textGone: "Loading", seconds: undefined },
    ]);
  });

  // 目的：宿主是进程边界上唯一的校验点；潜在缺陷：后端 schema 放过的组合在页面上执行出意外动作。
  it("rejects malformed actions", () => {
    const act = (action: Record<string, unknown>) => () => parseAct({ task_id: 7, actions: [action] });
    expect(codeOf(act({ type: "press", key: "F5" }))).toBe("INVALID_PARAMS");
    expect(codeOf(act({ type: "press", key: "K" }))).toBe("INVALID_PARAMS");
    expect(codeOf(act({ type: "press", key: "a", modifiers: ["Hyper"] }))).toBe("INVALID_PARAMS");
    expect(codeOf(act({ type: "scroll" }))).toBe("INVALID_PARAMS");
    expect(codeOf(act({ type: "wait_for", text: "a", text_gone: "b" }))).toBe("INVALID_PARAMS");
    expect(codeOf(act({ type: "wait_for" }))).toBe("INVALID_PARAMS");
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
    const refs = new RefRegistry();
    const snapshot = formatSnapshot(tree, refs);
    expect(snapshot.text).toBe([
      "- heading \"Welcome\" [level=1]",
      "  - text \"Welcome\"",
      "- text \"Hello there\"",
      "- textbox \"Email\" [ref=e1] [required] value=\"a@b.c\"",
      "- button \"Buy\" [ref=e2] [disabled]",
      "- iframe (contents not shown)",
    ].join("\n"));
    expect([refs.resolve("e1"), refs.resolve("e2")]).toEqual([61, 91]);
    expect(snapshot.frames).toBe(1);
  });

  // 目的：ref 按节点稳定；潜在缺陷：页面插入新元素后旧序号悄悄指到另一个元素，模型点错东西。
  it("keeps an element's ref across snapshots and never reuses numbers after navigation", () => {
    const refs = new RefRegistry();
    formatSnapshot(tree, refs);
    const grown = [
      ...tree.slice(0, 1),
      node("2", "generic", { parentId: "1", childIds: ["0", "6", "9"] }),
      node("0", "link", { parentId: "2", name: { value: "New" }, backendDOMNodeId: 5 }),
      ...tree.slice(6, 8),
    ];
    expect(formatSnapshot(grown, refs).text).toBe([
      "- link \"New\" [ref=e3]",
      "- textbox \"Email\" [ref=e1] [required] value=\"a@b.c\"",
      "- button \"Buy\" [ref=e2] [disabled]",
    ].join("\n"));

    refs.clear();
    expect(refs.resolve("e1")).toBeUndefined();
    expect(formatSnapshot(tree, refs).text).toContain("[ref=e4]");
  });

  // 目的：快照自己守预算且能接着读；潜在缺陷：长页面后半段的元素模型看不到也点不到。
  it("cuts at whole lines and tells where to read on", () => {
    const refs = new RefRegistry();
    const first = formatSnapshot(tree, refs, { budget: 60 }).text;
    const note = first.split("\n").at(-1)!;
    expect(note).toMatch(/^… \d+ more lines not shown; read on with from_line=\d+$/);
    const next = Number(note.match(/from_line=(\d+)/)![1]);
    const rest = formatSnapshot(tree, refs, { fromLine: next }).text;
    expect(rest.split("\n")[0]).toBe(`… lines 1-${next - 1} not shown (from_line=${next})`);
    expect(rest).toContain("- iframe (contents not shown)");
    expect(formatSnapshot(tree, refs, { fromLine: 99 }).text).toBe("(the snapshot has only 6 lines)");
  });

  it("describes an empty page", () => {
    expect(formatSnapshot([node("1", "RootWebArea")], new RefRegistry()).text).toBe("(the page has no readable content)");
  });
});

/** 记下每条 CDP 命令；页面函数的返回值由 `answers` 按函数片段决定，默认空串（成功）。 */
function fakeSession(answers: Record<string, unknown> = {}): { session: PageSession; calls: [string, Record<string, unknown>][] } {
  const calls: [string, Record<string, unknown>][] = [];
  const answer = (code: string) => Object.entries(answers).find(([fragment]) => code.includes(fragment))?.[1] ?? "";
  const session: PageSession = {
    cdp: async <T,>(method: string, params: Record<string, unknown> = {}) => {
      calls.push([method, params]);
      if (method === "DOM.resolveNode") return { object: { objectId: "obj" } } as T;
      if (method === "DOM.getBoxModel") return { model: { content: [0, 0, 10, 0, 10, 20, 0, 20] } } as T;
      if (method === "Runtime.callFunctionOn") return { result: { value: answer(String(params.functionDeclaration)) } } as T;
      if (method === "Runtime.evaluate") return { result: { value: answer(String(params.expression)) } } as T;
      return {} as T;
    },
    resolveRef: (ref) => {
      if (ref === "e404") throw new HostMethodError("REF_STALE", "not registered");
      return Number(ref.slice(1));
    },
    settle: async () => undefined,
  };
  return { session, calls };
}

const run = (session: PageSession, ...actions: PageAction[]) => runActions(session, actions, new AbortController().signal);

describe("runActions", () => {
  // 目的：稳定 ref 可能来自更早的快照；潜在缺陷：焦点落在已脱离文档的节点上，字输进别的元素。
  it("refuses refs whose element left the document", async () => {
    const { session, calls } = fakeSession({ isConnected: "detached" });
    const outcome = await run(session, { type: "type", ref: "e7", text: "hi", clear: false, submit: false });
    expect(outcome).toMatchObject({ completed: 0, failure: { index: 0, code: "REF_STALE" } });
    expect(calls.some(([method]) => method === "Input.insertText")).toBe(false);
  });

  it("hovers by moving the mouse to the element's centre without clicking", async () => {
    const { session, calls } = fakeSession();
    await run(session, { type: "hover", ref: "e2" });
    const mouse = calls.filter(([method]) => method === "Input.dispatchMouseEvent").map(([, params]) => params);
    expect(mouse).toEqual([{ type: "mouseMoved", x: 5, y: 10 }]);
  });

  it("presses shortcuts with modifier bits and no character, after focusing the ref", async () => {
    const { session, calls } = fakeSession();
    await run(session, { type: "press", key: "k", modifiers: ["ControlOrMeta", "Shift"], ref: "e3" });
    const keys = calls.filter(([method]) => method === "Input.dispatchKeyEvent").map(([, params]) => params);
    const meta = process.platform === "darwin" ? 4 : 2;
    expect(keys[0]).toMatchObject({ type: "keyDown", key: "K", code: "KeyK", modifiers: meta | 8 });
    expect(keys[0]).not.toHaveProperty("text");
    expect(calls.findIndex(([method]) => method === "DOM.focus")).toBeLessThan(calls.findIndex(([method]) => method === "Input.dispatchKeyEvent"));

    const typed = fakeSession();
    await run(typed.session, { type: "press", key: "a", modifiers: ["Shift"] });
    expect(typed.calls.find(([method]) => method === "Input.dispatchKeyEvent")![1]).toMatchObject({ key: "A", text: "A", modifiers: 8 });
  });

  // 目的：无限滚动到底要停下；潜在缺陷：批次里的滚动空转，模型以为加载了更多。
  it("scrolls the area around a ref and stops with SCROLL_END when nothing moves", async () => {
    const { session, calls } = fakeSession({ "scrollBy": "this area is already at the bottom" });
    const outcome = await run(session, { type: "scroll", direction: "down", ref: "e9" }, { type: "click", ref: "e1" });
    expect(outcome).toMatchObject({ completed: 0, failure: { index: 0, code: "SCROLL_END", message: "this area is already at the bottom" } });
    const scrolled = calls.find(([method, params]) => method === "Runtime.callFunctionOn" && String(params.functionDeclaration).includes("scrollBy"));
    expect(scrolled?.[1].arguments).toEqual([{ value: true }]);

    const page = fakeSession();
    expect(await run(page.session, { type: "scroll", direction: "up" })).toMatchObject({ completed: 1, failure: null });
    expect(String(page.calls.find(([method]) => method === "Runtime.evaluate")![1].expression)).toContain(".call(document.documentElement, false)");

    const into = fakeSession();
    await run(into.session, { type: "scroll", ref: "e9" });
    expect(into.calls.at(-1)).toEqual(["DOM.scrollIntoViewIfNeeded", { backendNodeId: 9 }]);
  });

  it("waits for text to disappear and reports when it stays", async () => {
    const gone = fakeSession({ innerText: false });
    expect(await run(gone.session, { type: "wait_for", textGone: "Loading", seconds: 1 })).toMatchObject({ failure: null });

    const stays = fakeSession({ innerText: true });
    const outcome = await run(stays.session, { type: "wait_for", textGone: "Loading", seconds: 0.3 });
    expect(outcome.failure).toMatchObject({ code: "WAIT_TIMEOUT", message: 'the text "Loading" is still on the page after 0.3 seconds' });
  });
});
