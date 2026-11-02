import { describe, expect, it } from "vitest";

import type { BrowserDialog, BrowserDialogAnswer } from "@/electron/shared/host-contract";
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
  const base = { task_id: 7, limits: { per_task: 5, total: 20 }, download_dir: "/ws/.cosir/Download" };
  // 后端照 model_dump() 原样发送：参数模型的每个字段都在，未填的为 null（导出的 schema 按这个形状校验）。
  const navigate = (args: Record<string, unknown>) => ({ ...base, url: null, action: null, tab: null, new_tab: false, viewport: null, color_scheme: null, ...args });
  const snapshot = (args: Record<string, unknown>) => ({ ...base, tab: null, from_line: null, ...args });
  const wire = {
    hover: (ref: string) => ({ type: "hover", ref }),
    press: (args: Record<string, unknown>) => ({ type: "press", modifiers: null, ref: null, ...args }),
    scroll: (args: Record<string, unknown>) => ({ type: "scroll", direction: null, ref: null, ...args }),
    waitFor: (args: Record<string, unknown>) => ({ type: "wait_for", text: null, text_gone: null, seconds: null, ...args }),
    dialog: (args: Record<string, unknown>) => ({ type: "dialog", text: null, id: null, ...args }),
  };
  const act = (...actions: Record<string, unknown>[]) => ({ ...base, tab: null, actions });

  it("accepts a url or a history action, and the emulation to apply first", () => {
    expect(parseNavigate(navigate({ url: "https://example.com/a" }))).toMatchObject({ target: { url: "https://example.com/a" }, newTab: false });
    expect(parseNavigate(navigate({ url: "https://example.com/a" }))).not.toHaveProperty("emulation");
    expect(parseNavigate(navigate({ action: "back", tab: "t3" }))).toMatchObject({
      tab: "t3", target: { action: "back" }, context: { limits: { perTask: 5, total: 20 }, downloadDir: "/ws/.cosir/Download" },
    });
    expect(parseNavigate(navigate({ action: "reload", viewport: { width: 390, height: 844, mobile: true }, color_scheme: "auto" })).emulation)
      .toEqual({ width: 390, height: 844, mobile: true, colorScheme: null });
    expect(parseNavigate(navigate({ action: "reload", color_scheme: "dark" })).emulation).toEqual({ colorScheme: "dark" });
    expect(parseSnapshot(snapshot({}))).toEqual({
      taskId: 7, fromLine: 1, context: { limits: { perTask: 5, total: 20 }, downloadDir: "/ws/.cosir/Download" },
    });
    expect(parseSnapshot(snapshot({ from_line: 40 }))).toMatchObject({ fromLine: 40 });
    // 每个请求都带上下文（宿主据此刷新上限）；参数模型的字段一个都不能少。
    expect(codeOf(() => parseSnapshot({ task_id: 7, tab: null, from_line: null }))).toBe("INVALID_PARAMS");
    expect(codeOf(() => parseSnapshot({ ...base, tab: null }))).toBe("INVALID_PARAMS");
    expect(parseDropTasks({ task_ids: [1, 2] })).toEqual([1, 2]);
  });

  // 目的：Agent 只能打开网页；潜在缺陷：经浏览器读本机文件或执行 javascript: 地址。
  it("blocks non-http schemes and rejects malformed params", () => {
    expect(codeOf(() => parseNavigate(navigate({ url: "file:///etc/passwd" })))).toBe("NAVIGATION_BLOCKED");
    expect(codeOf(() => parseNavigate(navigate({ url: "javascript:alert(1)" })))).toBe("NAVIGATION_BLOCKED");
    expect(codeOf(() => parseNavigate(navigate({ url: "not a url" })))).toBe("INVALID_PARAMS");
    expect(codeOf(() => parseNavigate(navigate({ action: "home" })))).toBe("INVALID_PARAMS");
    expect(codeOf(() => parseNavigate(navigate({ url: "https://a.b", tab: "../t1" })))).toBe("INVALID_PARAMS");
    expect(codeOf(() => parseNavigate(navigate({ url: "https://a.b", limits: { per_task: 0, total: 1 } })))).toBe("INVALID_PARAMS");
    expect(codeOf(() => parseNavigate(navigate({ url: "https://a.b", download_dir: "relative/dir" })))).toBe("INVALID_PARAMS");
    expect(codeOf(() => parseNavigate(navigate({ url: "https://a.b", viewport: { width: 10, height: 844, mobile: false } })))).toBe("INVALID_PARAMS");
    expect(codeOf(() => parseSnapshot(snapshot({ from_line: 0 })))).toBe("INVALID_PARAMS");
  });

  it("maps the wire actions, turning null into unset", () => {
    const { actions } = parseAct(act(
      wire.hover("e3"),
      wire.press({ key: "k", modifiers: ["ControlOrMeta"], ref: "e4" }),
      wire.press({ key: "Enter" }),
      wire.scroll({ direction: "down", ref: "e5" }),
      wire.scroll({ ref: "e5" }),
      wire.waitFor({ text_gone: "Loading" }),
      wire.dialog({ accept: true }),
      wire.dialog({ accept: true, text: "Ada", id: 4 }),
    ));
    expect(actions).toEqual([
      { type: "hover", ref: "e3" },
      { type: "press", key: "k", modifiers: ["ControlOrMeta"], ref: "e4" },
      { type: "press", key: "Enter", modifiers: [] },
      { type: "scroll", direction: "down", ref: "e5" },
      { type: "scroll", direction: undefined, ref: "e5" },
      { type: "wait_for", text: undefined, textGone: "Loading", seconds: undefined },
      { type: "dialog", accept: true, text: undefined, id: undefined },
      { type: "dialog", accept: true, text: "Ada", id: 4 },
    ]);
  });

  // 目的：宿主按导出的契约校验形状、枚举与上限；潜在缺陷：后端放过的取值在页面上执行出意外动作。跨字段规则
  // （scroll 要方向或 ref、wait_for 的组合）只在后端参数模型里，由后端测试守着，宿主信任后端。
  it("rejects actions outside the exported contract", () => {
    const one = (action: Record<string, unknown>) => () => parseAct(act(action));
    expect(codeOf(one(wire.press({ key: "F5" })))).toBe("INVALID_PARAMS");
    expect(codeOf(one(wire.press({ key: "K" })))).toBe("INVALID_PARAMS");
    expect(codeOf(one(wire.press({ key: "a", modifiers: ["Hyper"] })))).toBe("INVALID_PARAMS");
    expect(codeOf(one({ type: "dialog", text: null, id: null }))).toBe("INVALID_PARAMS");
    expect(codeOf(one(wire.dialog({ accept: true, text: 3 })))).toBe("INVALID_PARAMS");
    expect(codeOf(one(wire.dialog({ accept: true, id: 0 })))).toBe("INVALID_PARAMS");
    expect(codeOf(one({ type: "hover" }))).toBe("INVALID_PARAMS");
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

/**
 * 记下每条 CDP 命令；页面函数的返回值由 `answers` 按函数片段决定，默认空串（成功）。
 * `dialogOn` 选中的那条命令上页面弹窗：宿主让它以 DIALOG_OPEN 失败（页面停在事件处理里，命令回不来）。
 */
function fakeSession(
  answers: Record<string, unknown> = {},
  { scale = 1, dialogOn, initialDialog = null }: {
    scale?: number;
    dialogOn?: (method: string, params: Record<string, unknown>) => boolean;
    initialDialog?: BrowserDialog | null;
  } = {},
): { session: PageSession; calls: [string, Record<string, unknown>][]; answered: [number, BrowserDialogAnswer][] } {
  const calls: [string, Record<string, unknown>][] = [];
  const answered: [number, BrowserDialogAnswer][] = [];
  let dialog: BrowserDialog | null = initialDialog;
  const answer = (code: string) => Object.entries(answers).find(([fragment]) => code.includes(fragment))?.[1] ?? "";
  const paused = () => (dialog ? new HostMethodError("DIALOG_OPEN", `the page is paused: it is showing a ${dialog.type} dialog (#${dialog.id})`) : null);
  const session: PageSession = {
    cdp: async <T,>(method: string, params: Record<string, unknown> = {}) => {
      calls.push([method, params]);
      if (dialogOn?.(method, params)) {
        dialog = { id: 7, type: "confirm", message: "delete?", defaultText: "" };
        throw paused();
      }
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
    toInput: ({ x, y }) => ({ x: x * scale, y: y * scale }),
    paused,
    dialog: () => dialog,
    answerDialog: (id, reply) => {
      dialog = null;
      answered.push([id, reply]);
    },
  };
  return { session, calls, answered };
}

const mouse = (type: string) => (method: string, params: Record<string, unknown>) => method === "Input.dispatchMouseEvent" && params.type === type;

const run = (session: PageSession, ...actions: PageAction[]) => runActions(session, actions, new AbortController().signal);

describe("runActions", () => {
  // 目的：面板缩放显示固定视口时，CDP 合成输入的坐标是视图坐标；潜在缺陷：不换算就点到别处（实测缩放 0.5 时落点翻倍）。
  it("converts element centres from CSS pixels to input coordinates", async () => {
    const { session, calls } = fakeSession({}, { scale: 0.5 });
    await run(session, { type: "click", ref: "e2" });
    const mouse = calls.filter(([method]) => method === "Input.dispatchMouseEvent").map(([, params]) => [params.x, params.y]);
    expect(mouse).toEqual([[2.5, 5], [2.5, 5], [2.5, 5]]);
  });

  // 目的：点删除弹出确认、同批确认；潜在缺陷：弹窗让 click 被误判失败，或后续动作挂到 CDP 超时。
  it("treats a dialog opened on an action's effect command as its effect and answers it in the same batch", async () => {
    const { session, answered } = fakeSession({}, { dialogOn: mouse("mouseReleased") });
    const outcome = await run(session, { type: "click", ref: "e2" }, { type: "dialog", accept: true });
    expect(outcome).toEqual({ completed: 2, failure: null });
    expect(answered).toEqual([[7, { accept: true, text: undefined }]]);
  });

  // 目的：只有动作自己触发的弹窗才算动作生效；潜在缺陷：页面在别的时机弹窗时，点击没发生、等待没等到却报完成，
  // 同批的 dialog 还会把一个没看见的弹窗答掉。
  it("fails an action when the dialog appears before its effect, and does not count a wait as done", async () => {
    const hovered = fakeSession({}, { dialogOn: mouse("mouseMoved") });
    const click = await run(hovered.session, { type: "click", ref: "e2" }, { type: "dialog", accept: true });
    expect(click).toMatchObject({ completed: 0, failure: { index: 0, code: "DIALOG_OPEN" } });
    expect(hovered.answered).toEqual([]);

    const waiting = fakeSession({}, { dialogOn: (method) => method === "Runtime.evaluate" });
    const wait = await run(waiting.session, { type: "wait_for", text: "Done", seconds: 1 });
    expect(wait).toMatchObject({ completed: 0, failure: { index: 0, code: "DIALOG_OPEN" } });
  });

  // 目的：作答必须对准看见的那个弹窗；潜在缺陷：调用开始时已有的弹窗被一个不带编号的作答答掉，或答给了换上来的新弹窗。
  it("answers a dialog that was already open only by its id", async () => {
    const initialDialog = { id: 3, type: "alert" as const, message: "hi", defaultText: "" };
    const unnamed = fakeSession({}, { initialDialog });
    expect(await run(unnamed.session, { type: "dialog", accept: true })).toMatchObject({ completed: 0, failure: { code: "DIALOG_MISMATCH" } });
    expect(await run(unnamed.session, { type: "dialog", accept: true, id: 4 })).toMatchObject({ completed: 0, failure: { code: "DIALOG_MISMATCH" } });
    expect(unnamed.answered).toEqual([]);
    expect(await run(unnamed.session, { type: "dialog", accept: true, id: 3 })).toEqual({ completed: 1, failure: null });
    expect(unnamed.answered).toEqual([[3, { accept: true, text: undefined }]]);
  });

  it("stops other actions while the page is paused, and refuses to answer a dialog that is not there", async () => {
    const opened = fakeSession({}, { dialogOn: mouse("mouseReleased") });
    const blocked = await run(opened.session, { type: "click", ref: "e2" }, { type: "hover", ref: "e2" });
    expect(blocked).toMatchObject({ completed: 1, failure: { index: 1, code: "DIALOG_OPEN" } });

    const none = await run(fakeSession().session, { type: "dialog", accept: false });
    expect(none).toMatchObject({ completed: 0, failure: { index: 0, code: "NO_DIALOG" } });
  });

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
