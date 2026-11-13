import os from "node:os";
import { describe, expect, it, vi } from "vitest";

import type { BrowserDialog, BrowserDialogAnswer } from "@/electron/shared/host-contract";
import { HostMethodError } from "@/electron/main/host-link/method-registry";
import { runActions } from "@/electron/main/browser/input/actions";
import type { Box } from "@/electron/main/browser/input/element";
import { BrowserOperation, type FileChooser } from "@/electron/main/browser/operation";
import type { BrowserTab } from "@/electron/main/browser/browser-tab";
import type { SendOptions } from "@/electron/main/browser/cdp-session";
vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));
import { parseAct, parseDropTasks, parseNavigate, parseSnapshot, type PageAction } from "@/electron/main/browser/params";
import { addressToUrl, isPageUrl, pageUrl } from "@/electron/main/browser/page-url";
import { describeTarget } from "@/electron/main/browser/input/targets";
import { RefRegistry } from "@/electron/main/browser/ref-registry";
import type { AXNode, SnapshotDocument } from "@/electron/main/browser/snapshot/document";
import { documentLayout, frameRegion, pointerRoots, visibleNodes, type DomSnapshot } from "@/electron/main/browser/snapshot/layout";
import { deviceRegion, geometryOf, overlayQuad } from "@/electron/main/browser/page-geometry";
import { buildOutline } from "@/electron/main/browser/snapshot/outline";
import { changesView, snapshotView } from "@/electron/main/browser/snapshot/views";


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
  const snapshot = (args: Record<string, unknown>) => ({ ...base, tab: null, scope: "page", find: null, from_line: null, ...args });
  const wire = {
    hover: (ref: string) => ({ type: "hover", ref, x: null, y: null }),
    click: (args: Record<string, unknown>) => ({ type: "click", ref: null, x: null, y: null, button: "left", click_count: 1, modifiers: null, ...args }),
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
      .toEqual({ viewport: { width: 390, height: 844, mobile: true }, colorScheme: null });
    // viewport 的 auto 回到跟随面板。
    expect(parseNavigate(navigate({ action: "reload", viewport: "auto" })).emulation).toEqual({ viewport: "follow" });
    expect(parseNavigate(navigate({ action: "reload", color_scheme: "dark" })).emulation).toEqual({ colorScheme: "dark" });
    expect(parseSnapshot(snapshot({}))).toEqual({
      taskId: 7, handoffTabs: [], view: { scope: "page" }, context: { limits: { perTask: 5, total: 20 }, downloadDir: "/ws/.cosir/Download" },
    });
    expect(parseSnapshot(snapshot({ from_line: 40, scope: "viewport", find: "Buy" }))).toMatchObject({ view: { scope: "viewport", find: "Buy", fromLine: 40 } });
    // 每个请求都带上下文（宿主据此刷新上限）；参数模型的字段一个都不能少。
    expect(codeOf(() => parseSnapshot({ task_id: 7, tab: null, scope: "page", find: null, from_line: null }))).toBe("INVALID_PARAMS");
    expect(codeOf(() => parseSnapshot({ ...base, tab: null }))).toBe("INVALID_PARAMS");
    expect(parseDropTasks({ task_ids: [1, 2] })).toEqual([1, 2]);
  });

  // 目的：Agent 能打开网页与本机文件、写法与地址栏一致；潜在缺陷：生成的 HTML 打不开，或执行 javascript: 地址。
  it("opens web pages and local files, blocks other schemes and rejects malformed params", () => {
    expect(parseNavigate(navigate({ url: "file:///tmp/a.html" })).target).toEqual({ url: "file:///tmp/a.html" });
    expect(parseNavigate(navigate({ url: "/tmp/a b.html" })).target).toEqual({ url: "file:///tmp/a%20b.html" });
    expect(parseNavigate(navigate({ url: "example.com" })).target).toEqual({ url: "https://example.com/" });
    expect(codeOf(() => parseNavigate(navigate({ url: "javascript:alert(1)" })))).toBe("NAVIGATION_BLOCKED");
    expect(codeOf(() => parseNavigate(navigate({ url: "vscode://open" })))).toBe("NAVIGATION_BLOCKED");
    expect(codeOf(() => parseNavigate(navigate({ url: "https://exa mple.com" })))).toBe("INVALID_PARAMS");
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
      wire.click({ x: 12.5, y: 40, button: "right", click_count: 2, modifiers: ["Shift"] }),
      { type: "drag", start: { ref: "e1", x: null, y: null }, end: { ref: null, x: 3, y: 4 } },
      wire.press({ key: "k", modifiers: ["ControlOrMeta"], ref: "e4" }),
      wire.press({ key: "Enter" }),
      wire.scroll({ direction: "down", ref: "e5" }),
      wire.scroll({ ref: "e5" }),
      wire.waitFor({ text_gone: "Loading" }),
      wire.dialog({ accept: true }),
      wire.dialog({ accept: true, text: "Ada", id: 4 }),
    ));
    expect(actions).toEqual([
      { type: "hover", target: { ref: "e3" } },
      { type: "click", target: { x: 12.5, y: 40 }, button: "right", clickCount: 2, modifiers: ["Shift"] },
      { type: "drag", start: { ref: "e1" }, end: { x: 3, y: 4 } },
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
    expect(codeOf(one(wire.click({ x: -1, y: 0 })))).toBe("INVALID_PARAMS");
    expect(codeOf(one(wire.click({ ref: "e1", button: "middle" })))).toBe("INVALID_PARAMS");
    expect(codeOf(one({ type: "upload", paths: ["report.pdf"], ref: null }))).toBe("INVALID_PARAMS");
  });
});

function node(nodeId: string, role: string, extra: Partial<AXNode> = {}): AXNode {
  return { nodeId, role: { value: role }, ...extra };
}

/** 大纲 + 整页视图：快照文本与没展开的子框架数。 */
function formatSnapshot(document: SnapshotDocument, refs: RefRegistry, options: { fromLine?: number; budget?: number; pageUrl?: string } = {}) {
  const outline = buildOutline(document, refs, { pageUrl: options.pageUrl });
  return { text: snapshotView(outline, options), frames: outline.frames };
}

/** 一份文档的读取结果（没有 pointer 元素、没有视口内的节点、没有展开的子框架，除非另给）。 */
function doc(nodes: AXNode[], extra: Partial<SnapshotDocument> = {}): SnapshotDocument {
  return { nodes, pointer: new Set(), inView: new Set(), frames: new Map(), ...extra };
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
    const snapshot = formatSnapshot(doc(tree), refs);
    expect(snapshot.text).toBe([
      "- heading \"Welcome\" [level=1]",
      "  - text \"Welcome\"",
      "- text \"Hello there\"",
      "- textbox \"Email\" [ref=e1] [required] value=\"a@b.c\"",
      "- button \"Buy\" [ref=e2] [disabled]",
      "- iframe (contents not shown)",
    ].join("\n"));
    expect([refs.resolve("e1"), refs.resolve("e2")]).toEqual([{ backendNodeId: 61 }, { backendNodeId: 91 }]);
    expect(snapshot.frames).toBe(1);
  });

  // 目的：ref 按节点稳定；潜在缺陷：页面插入新元素后旧序号悄悄指到另一个元素，模型点错东西。
  it("keeps an element's ref across snapshots and never reuses numbers after navigation", () => {
    const refs = new RefRegistry();
    formatSnapshot(doc(tree), refs);
    const grown = [
      ...tree.slice(0, 1),
      node("2", "generic", { parentId: "1", childIds: ["0", "6", "9"] }),
      node("0", "link", { parentId: "2", name: { value: "New" }, backendDOMNodeId: 5 }),
      ...tree.slice(6, 8),
    ];
    expect(formatSnapshot(doc(grown), refs).text).toBe([
      "- link \"New\" [ref=e3]",
      "- textbox \"Email\" [ref=e1] [required] value=\"a@b.c\"",
      "- button \"Buy\" [ref=e2] [disabled]",
    ].join("\n"));

    refs.clear();
    expect(refs.resolve("e1")).toBeUndefined();
    expect(formatSnapshot(doc(tree), refs).text).toContain("[ref=e4]");
  });

  // 目的：快照自己守预算且能接着读；潜在缺陷：长页面后半段的元素模型看不到也点不到。
  it("cuts at whole lines and tells where to read on", () => {
    const refs = new RefRegistry();
    const first = formatSnapshot(doc(tree), refs, { budget: 60 }).text;
    const note = first.split("\n").at(-1)!;
    expect(note).toMatch(/^… \d+ more lines not shown; read on with from_line=\d+$/);
    const next = Number(note.match(/from_line=(\d+)/)![1]);
    const rest = formatSnapshot(doc(tree), refs, { fromLine: next }).text;
    expect(rest.split("\n")[0]).toBe(`… lines 1-${next - 1} not shown (from_line=${next})`);
    expect(rest).toContain("- iframe (contents not shown)");
    expect(formatSnapshot(doc(tree), refs, { fromLine: 99 }).text).toBe("(the snapshot has only 6 lines)");
  });

  // 目的：iframe 里的元素也能看到、能操作；潜在缺陷：跨进程子框架的节点 id 与主框架重号，ref 指到主框架的元素上。
  it("splices frame contents under their iframe with refs kept apart per session", () => {
    const refs = new RefRegistry();
    const frame = doc([
      node("1", "RootWebArea", { childIds: ["2"] }),
      node("2", "button", { parentId: "1", name: { value: "Pay" }, backendDOMNodeId: 61 }),
    ], { sessionId: "S1" });
    const main = doc([
      node("1", "RootWebArea", { childIds: ["2", "3", "4"] }),
      node("2", "textbox", { parentId: "1", name: { value: "Email" }, backendDOMNodeId: 61 }),
      node("3", "Iframe", { parentId: "1", name: { value: "Checkout" }, backendDOMNodeId: 70 }),
      node("4", "Iframe", { parentId: "1", backendDOMNodeId: 80, ignored: true }),
    ], { frames: new Map([[70, frame]]) });
    const snapshot = formatSnapshot(main, refs);
    expect(snapshot.text).toBe([
      "- textbox \"Email\" [ref=e1]",
      "- iframe \"Checkout\"",
      "  - button \"Pay\" [ref=e2]",
    ].join("\n"));
    expect(snapshot.frames).toBe(0);
    expect(refs.resolve("e2")).toEqual({ sessionId: "S1", backendNodeId: 61 });

    // 读失败的子框架与没读的说法不同：模型与排查都分得清「超出上限」与「读不出来」。
    const failed = doc([
      node("1", "RootWebArea", { childIds: ["2", "3"] }),
      node("2", "Iframe", { parentId: "1", name: { value: "Ads" }, backendDOMNodeId: 70 }),
      node("3", "Iframe", { parentId: "1", name: { value: "Map" }, backendDOMNodeId: 80 }),
    ], { frames: new Map([[70, null]]) });
    const partial = formatSnapshot(failed, new RefRegistry());
    expect(partial.text).toBe('- iframe "Ads" (contents could not be read)\n- iframe "Map" (contents not shown)');
    expect(partial.frames).toBe(2);
  });

  // 目的：长页面上只读需要的部分；潜在缺陷：筛掉了祖先，模型分不清按钮属于哪条记录；或筛选后的 ref 与整页不是一套。
  it("shows only lines in view or matching a query, with their ancestors and the same refs", () => {
    const refs = new RefRegistry();
    const page = doc([
      node("1", "RootWebArea", { childIds: ["2", "5"] }),
      node("2", "list", { parentId: "1", name: { value: "Orders" }, backendDOMNodeId: 20, childIds: ["3", "4"] }),
      node("3", "button", { parentId: "2", name: { value: "Delete order 1" }, backendDOMNodeId: 30 }),
      node("4", "button", { parentId: "2", name: { value: "Delete order 2" }, backendDOMNodeId: 40 }),
      node("5", "link", { parentId: "1", name: { value: "Footer" }, backendDOMNodeId: 50 }),
    ], { inView: new Set([20, 30]) });
    const outline = buildOutline(page, refs);
    expect(snapshotView(outline, { scope: "viewport" }).split("\n")).toEqual([
      "(showing the part of the page in the viewport, with their ancestors: 2 of 4 lines)",
      "- list \"Orders\"",
      "  - button \"Delete order 1\" [ref=e1]",
    ]);
    expect(snapshotView(outline, { find: "order 2" }).split("\n").slice(1)).toEqual([
      "- list \"Orders\"",
      "  - button \"Delete order 2\" [ref=e2]",
    ]);
    expect(snapshotView(outline, { scope: "viewport", find: "footer" })).toBe("(nothing on the page: the part of the page in the viewport, lines matching \"footer\")");
  });

  // 目的：act 之后只给变化，省上下文；潜在缺陷：没变的行也回传，或漏掉文字变化、移位与删除，模型以为页面没变。
  it("lists new, changed, moved and removed lines against the last delivered outline", () => {
    const entry = (key: string, depth: number, text: string, parent: number) => ({ key, depth, text, parent, visible: true });
    const before = { frames: 0, entries: [
      entry("a", 0, 'list "Cart"', -1),
      entry("b", 1, 'listitem "Apple"', 0),
      entry("c", 2, 'button "Remove" [ref=e1]', 1),
      entry("d", 1, 'listitem "Pear"', 0),
      entry("e", 2, 'button "Remove" [ref=e2]', 3),
      entry("f", 0, 'text "Total 2"', -1),
      ...Array.from({ length: 10 }, (_, i) => entry(`z${i}`, 0, `text "footer ${i}"`, -1)),
    ] };
    const after = { frames: 0, entries: [
      entry("a", 0, 'list "Cart"', -1),
      entry("b", 1, 'listitem "Apple"', 0),
      entry("c", 2, 'button "Remove" [ref=e1]', 1),
      entry("f", 0, 'text "Total 1"', -1),
      entry("g", 0, 'status "Pear removed"', -1),
      ...Array.from({ length: 10 }, (_, i) => entry(`z${i}`, 0, `text "footer ${i}"`, -1)),
    ] };
    expect(changesView(before, after)!.split("\n")).toEqual([
      expect.stringMatching(/^\(changes since this tab's last browser result: 2 new or changed, 1 removed;/),
      '- text "Total 1" [changed]',
      '- status "Pear removed" [new]',
      "removed:",
      '- listitem "Pear"',
    ]);
    expect(changesView(after, after)).toBe("(nothing on the page changed since this tab's last browser result)");
    // 变化太多（这里是整页换了一遍）就交给调用方给整页。
    expect(changesView(before, { frames: 0, entries: [entry("x", 0, "heading", -1)] })).toBeNull();
  });

  it("describes an empty page", () => {
    expect(formatSnapshot(doc([node("1", "RootWebArea")]), new RefRegistry()).text).toBe("(the page has no readable content)");
  });

  // 目的：「看起来可点」的无语义元素也有把手；潜在缺陷：`<div onclick>` 被当成容器折叠掉，模型只能读到文字、点不到它。
  it("gives refs to clickable generic elements and shows link addresses", () => {
    const refs = new RefRegistry();
    const page = [
      node("1", "RootWebArea", { childIds: ["2", "3", "4"] }),
      node("2", "generic", { parentId: "1", backendDOMNodeId: 20, childIds: ["2a"] }),
      node("2a", "StaticText", { parentId: "2", name: { value: "Open card" } }),
      node("3", "link", { parentId: "1", name: { value: "Next" }, backendDOMNodeId: 30, properties: [{ name: "url", value: { value: "https://shop.test/next?q=1#top" } }] }),
      node("4", "link", { parentId: "1", name: { value: "Docs" }, backendDOMNodeId: 40, properties: [{ name: "url", value: { value: `https://docs.test/${"a".repeat(200)}` } }] }),
    ];
    const text = formatSnapshot(doc(page, { pointer: new Set([20, 30]) }), refs, { pageUrl: "https://shop.test/home" }).text;
    expect(text.split("\n").slice(0, 3)).toEqual([
      "- generic [ref=e1] [cursor=pointer]",
      "  - text \"Open card\"",
      "- link \"Next\" [ref=e2] url=\"/next?q=1#top\"",
    ]);
    expect(text).toMatch(/link "Docs" \[ref=e3\] url="https:\/\/docs\.test\/a{90,}…"$/);
    expect(refs.resolve("e1")).toEqual({ backendNodeId: 20 });
  });
});

describe("pointerRoots", () => {
  // 目的：cursor 会继承给子元素；潜在缺陷：一张卡片里每一层都拿到 ref，挤掉快照预算、模型不知道点哪层。
  it("keeps only the outermost pointer element, looking past nodes without layout", () => {
    // 节点：0 文档、1 卡片(pointer)、2 卡片里的 span(继承 pointer)、3 display:contents 包裹层(无布局)、
    // 4 包裹层里的按钮(pointer)、5 另一个普通 div。
    const snapshot: DomSnapshot = {
      strings: ["auto", "pointer"],
      documents: [{
        frameId: 0,
        nodes: { parentIndex: [-1, 0, 1, 1, 3, 0], backendNodeId: [100, 101, 102, 103, 104, 105] },
        layout: { nodeIndex: [0, 1, 2, 4, 5], styles: [[0], [1], [1], [1], [0]], bounds: [[0, 0, 1, 1], [0, 0, 1, 1], [0, 0, 1, 1], [0, 0, 1, 1], [0, 0, 1, 1]] },
      }],
    };
    expect([...pointerRoots(snapshot)]).toEqual([101]);
  });
});

describe("page geometry", () => {
  // 目的：截图、坐标落点、高亮、快照几何对「视口」与「像素比」的认识一致；潜在缺陷：手机模拟下没写 viewport 声明的
  // 网页按 980 宽排版，越界检查却按模拟的 390 判断，照截图给的坐标被拒（实测）。
  it("takes the visible viewport and pixel ratio from one layout reading", () => {
    // 实测数字：手机模拟 390 宽，网页按 980 宽排版、缩小显示（页面缩放约 0.398，这里取 0.4），Retina 屏。
    const mobile = geometryOf({
      layoutViewport: { clientWidth: 1960 },
      cssLayoutViewport: { clientWidth: 980 },
      cssVisualViewport: { pageX: 0, pageY: 0, clientWidth: 980, clientHeight: 2120.8, scale: 0.4 },
      cssContentSize: { width: 980, height: 2121 },
    });
    expect(mobile).toMatchObject({ visual: { width: 980, height: 2120.8 }, deviceRatio: 2, pageScale: 0.4 });
    expect(deviceRegion(mobile)).toEqual({ x: 0, y: 0, width: 1960, height: 4241.6 });
    // 高亮框按 CSS 像素乘页面缩放、除以像素比（实测：页面缩放约 0.4 时不乘就画到可视区域之外）。
    expect(overlayQuad({ x: 600, y: 300, width: 200, height: 100 }, mobile)).toEqual([120, 60, 160, 60, 160, 80, 120, 80]);
  });
});

describe("viewport geometry", () => {
  // 数字取自实测（Retina，像素比 2）：页面滚到 400，按钮在文档 (100, 700)，同进程 iframe 在 (300, 650)、自己滚了 5，
  // 里面的按钮在它的文档 (10, 20)。元素框与滚动偏移都是设备像素、各自文档的坐标。
  const snapshot: DomSnapshot = {
    strings: ["main-frame", "child-frame"],
    documents: [
      {
        frameId: 0, scrollOffsetX: 0, scrollOffsetY: 800,
        nodes: { backendNodeId: [1, 2, 3, 4] },
        layout: { nodeIndex: [0, 1, 2, 3], styles: [], bounds: [[0, 0, 2560, 6000], [200, 1400, 160, 60], [600, 1300, 600, 300], [200, 3000, 160, 60]] },
      },
      {
        frameId: 1, scrollOffsetX: 0, scrollOffsetY: 10,
        nodes: { backendNodeId: [10, 11, 12] },
        layout: { nodeIndex: [0, 1, 2], styles: [], bounds: [[0, 0, 600, 1000], [20, 40, 100, 40], [20, 900, 100, 40]] },
      },
    ],
  };
  const metrics = {
    layoutViewport: { clientWidth: 2560 },
    cssLayoutViewport: { clientWidth: 1280 },
    cssVisualViewport: { pageX: 0, pageY: 400, clientWidth: 1280, clientHeight: 800, scale: 1 },
    cssContentSize: { width: 1280, height: 3000 },
  };

  // 目的：「只看视口」只列看得见的节点；潜在缺陷：把 CSS 像素当设备像素比（Retina 上差一倍），或忘了减滚动、
  // 忘了子框架坐标相对它自己的文档，列出的是另一块区域。
  it("finds the nodes in view in the main document and inside a scrolled iframe", () => {
    const region = deviceRegion(geometryOf(metrics));
    expect(region).toEqual({ x: 0, y: 800, width: 2560, height: 1600 });
    const main = documentLayout(snapshot)!;
    expect([...visibleNodes(main, region)].sort()).toEqual([1, 2, 3]);
    const child = documentLayout(snapshot, "child-frame")!;
    const childRegion = frameRegion(main, region, 3, child);
    expect(childRegion).toEqual({ x: 0, y: 10, width: 600, height: 300 });
    expect([...visibleNodes(child, childRegion)].sort()).toEqual([10, 11]);
    expect(frameRegion(main, { x: 0, y: 0, width: 2560, height: 1000 }, 3, child)).toBeNull();
    expect(documentLayout(snapshot, "gone")).toBeUndefined();
  });
});

/** 一条发出的 CDP 命令；发到跨进程子框架会话的另带会话 id。 */
type Call = [string, Record<string, unknown>, string?];

/**
 * 记下每条发出的 CDP 命令；页面函数的返回值由 `answers` 按函数片段决定，默认空串（成功）。
 * `dialogOn` 选中的那条命令上页面弹窗：宿主让它以 DIALOG_OPEN 失败（页面停在事件处理里，命令回不来）。
 * `dragData` 给出时页面可拖放：开着拦截时按住移动会发 `Input.dragIntercepted`。
 */
function fakeSession(
  answers: Record<string, unknown> = {},
  { scale = 1, dialogOn, initialDialog = null, dragData, fileChooser = null, shown = false, failOn }: {
    scale?: number;
    /** 这条命令失败（抛出给定的错误）：模拟 CDP 报节点不在、没有布局，或页面停住、超时。 */
    failOn?: { method: string; error: Error };
    /** 标签挂在面板上：动作前会画高亮（记进 `highlighted`）。 */
    shown?: boolean;
    fileChooser?: FileChooser | null;
    dragData?: unknown;
    dialogOn?: (method: string, params: Record<string, unknown>) => boolean;
    initialDialog?: BrowserDialog | null;
  } = {},
): { session: BrowserOperation; calls: Call[]; highlighted: Box[]; answered: [number, BrowserDialogAnswer][] } {
  const calls: Call[] = [];
  const highlighted: Box[] = [];
  const answered: [number, BrowserDialogAnswer][] = [];
  let dialog: BrowserDialog | null = initialDialog;
  // 按代码片段给页面函数的返回值；给的是 Error 时模拟页面里抛出异常（CDP 以 `exceptionDetails` 回报，不报错）。
  const answer = (code: string) => Object.entries(answers).find(([fragment]) => code.includes(fragment))?.[1] ?? "";
  const paused = () => (dialog ? new HostMethodError("DIALOG_OPEN", `the page is paused: it is showing a ${dialog.type} dialog (#${dialog.id})`) : null);
  const listeners = new Map<string, Set<(params: never) => void>>();
  let intercepting = false;
  const capabilities: Pick<BrowserOperation, "cdp" | "resolveRef" | "frameOffset" | "on" | "settle" | "toInput" | "geometry" | "fileChooser" | "highlight" | "paused" | "dialog" | "answerDialog"> = {
    cdp: async <T,>(method: string, params: Record<string, unknown> = {}, options: { browserSide?: boolean; sessionId?: string } = {}) => {
      // 与 `CdpSession` 的闸门一致：页面停着时只有浏览器进程作答的命令能回复。
      if (dialog && !options.browserSide) throw paused();
      calls.push(options.sessionId ? [method, params, options.sessionId] : [method, params]);
      if (method === "Input.setInterceptDrags") intercepting = params.enabled as boolean;
      if (intercepting && dragData && method === "Input.dispatchMouseEvent" && params.buttons === 1) {
        for (const handler of listeners.get("Input.dragIntercepted") ?? []) handler({ data: dragData } as never);
      }
      if (dialogOn?.(method, params)) {
        dialog = { id: 7, type: "confirm", message: "delete?", defaultText: "" };
        throw paused();
      }
      if (failOn?.method === method) throw failOn.error;
      if (method === "DOM.resolveNode") return { object: { objectId: "obj" } } as T;
      if (method === "DOM.getBoxModel") return { model: { border: [0, 0, 10, 0, 10, 20, 0, 20] } } as T;
      if (method === "Runtime.callFunctionOn" || method === "Runtime.evaluate") {
        const value = answer(String(params.functionDeclaration ?? params.expression));
        return (value instanceof Error
          ? { result: { type: "object" }, exceptionDetails: { text: "Uncaught", exception: { description: `${value.name}: ${value.message}\n    at <anonymous>:1:1` } } }
          : { result: { value } }) as T;
      }
      return {} as T;
    },
    // `f` 开头的 ref 在跨进程子框架 S1 里，S1 的视口原点在主视口的 (100, 200)。
    resolveRef: (ref) => {
      if (ref === "e404") throw new HostMethodError("REF_STALE", "not registered");
      return ref.startsWith("f") ? { sessionId: "S1", backendNodeId: Number(ref.slice(1)) } : { backendNodeId: Number(ref.slice(1)) };
    },
    frameOffset: async (sessionId) => (sessionId === "S1" ? { x: 100, y: 200 } : { x: 0, y: 0 }),
    on: (method, handler) => {
      const set = listeners.get(method) ?? new Set();
      set.add(handler);
      listeners.set(method, set);
      return () => set.delete(handler);
    },
    settle: async () => undefined,
    toInput: ({ x, y }) => ({ x: x * scale, y: y * scale }),
    geometry: async () => ({ visual: { x: 0, y: 0, width: 100, height: 50 }, deviceRatio: 1, pageScale: 1, content: { width: 100, height: 50 } }),
    fileChooser: () => fileChooser,
    // 与 `TabHighlight.show` 一致：不在面板上不量位置；量不到就不画、不抛错。
    highlight: async (locate) => {
      if (!shown) return;
      try {
        highlighted.push(await locate());
      } catch { /* 动作自己会报同样的问题 */ }
    },
    paused,
    dialog: () => dialog,
    answerDialog: (id, reply) => {
      dialog = null;
      answered.push([id, reply]);
    },
  };
  const tab = {
    taskId: 1, tabId: "t1", documentGeneration: 0,
    contents: { isDestroyed: () => false }, noteAgentInput() {}, operationChanged() {}, operationSettled() {},
    refLabel: () => undefined,
    session: { send: <T,>(method: string, params: Record<string, unknown>, options: SendOptions) => {
      options.signal?.throwIfAborted();
      options.onDispatch?.();
      const raw = capabilities.cdp<T>(method, params, options);
      options.onCommand?.(raw);
      return raw;
    } },
  } as unknown as BrowserTab;
  const session = new BrowserOperation(tab, new AbortController().signal);
  Object.assign(session, { ...capabilities, cdp: session.cdp });
  return { session, calls, highlighted, answered };
}

const mouse = (type: string) => (method: string, params: Record<string, unknown>) => method === "Input.dispatchMouseEvent" && params.type === type;

const clickOn = (ref: string): PageAction => ({ type: "click", target: { ref }, button: "left", clickCount: 1, modifiers: [] });

const run = (session: BrowserOperation, ...actions: PageAction[]) => runActions(session, actions, new AbortController().signal);

describe("runActions", () => {
  // 目的：面板缩放显示固定视口时，CDP 合成输入的坐标是视图坐标；潜在缺陷：不换算就点到别处（实测缩放 0.5 时落点翻倍）。
  it("converts element centres from CSS pixels to input coordinates", async () => {
    const { session, calls } = fakeSession({}, { scale: 0.5 });
    await run(session, clickOn("e2"));
    const mouse = calls.filter(([method]) => method === "Input.dispatchMouseEvent").map(([, params]) => [params.x, params.y]);
    expect(mouse).toEqual([[2.5, 5], [2.5, 5], [2.5, 5]]);
  });

  // 目的：点删除弹出确认、同批确认；潜在缺陷：弹窗让 click 被误判失败，或后续动作挂到 CDP 超时。
  it("treats a dialog opened on an action's effect command as its effect and answers it in the same batch", async () => {
    const { session, answered } = fakeSession({}, { dialogOn: mouse("mouseReleased") });
    const outcome = await run(session, clickOn("e2"), { type: "dialog", accept: true });
    expect(outcome).toMatchObject({ steps: [{ status: "unknown" }, { status: "done" }], interruption: { code: "DIALOG_OPEN" } });
    expect(answered).toEqual([[7, { accept: true, text: undefined }]]);
  });

  // 目的：只有动作自己触发的弹窗才算动作生效；潜在缺陷：页面在别的时机弹窗时，点击没发生、等待没等到却报完成，
  // 同批的 dialog 还会把一个没看见的弹窗答掉。
  it("fails an action when the dialog appears before its effect, and does not count a wait as done", async () => {
    const hovered = fakeSession({}, { dialogOn: mouse("mouseMoved") });
    const click = await run(hovered.session, clickOn("e2"), { type: "dialog", accept: true });
    expect(click).toMatchObject({ interruption: { code: "DIALOG_OPEN" } });
    expect(hovered.answered).toEqual([]);

    const waiting = fakeSession({}, { dialogOn: (method) => method === "Runtime.evaluate" });
    const wait = await run(waiting.session, { type: "wait_for", text: "Done", seconds: 1 });
    expect(wait).toMatchObject({ interruption: { code: "DIALOG_OPEN" } });
  });

  // 目的：作答必须对准看见的那个弹窗；潜在缺陷：调用开始时已有的弹窗被一个不带编号的作答答掉，或答给了换上来的新弹窗。
  it("answers a dialog that was already open only by its id", async () => {
    const initialDialog = { id: 3, type: "alert" as const, message: "hi", defaultText: "" };
    const unnamed = fakeSession({}, { initialDialog });
    expect(await run(unnamed.session, { type: "dialog", accept: true })).toMatchObject({ interruption: { code: "DIALOG_MISMATCH" } });
    expect(await run(unnamed.session, { type: "dialog", accept: true, id: 4 })).toMatchObject({ interruption: { code: "DIALOG_MISMATCH" } });
    expect(unnamed.answered).toEqual([]);
    expect(await run(unnamed.session, { type: "dialog", accept: true, id: 3 })).toMatchObject({ steps: Array(1).fill({ status: "done" }), interruption: null });
    expect(unnamed.answered).toEqual([[3, { accept: true, text: undefined }]]);
  });

  it("stops other actions while the page is paused, and refuses to answer a dialog that is not there", async () => {
    const opened = fakeSession({}, { dialogOn: mouse("mouseReleased") });
    const blocked = await run(opened.session, clickOn("e2"), { type: "hover", target: { ref: "e2" } });
    expect(blocked).toMatchObject({ interruption: { code: "DIALOG_OPEN" } });

    const none = await run(fakeSession().session, { type: "dialog", accept: false });
    expect(none).toMatchObject({ interruption: { code: "NO_DIALOG" } });
  });

  // 目的：稳定 ref 可能来自更早的快照；潜在缺陷：焦点落在已脱离文档的节点上，字输进别的元素。
  it("refuses refs whose element left the document", async () => {
    const { session, calls } = fakeSession({ isConnected: "detached" });
    const outcome = await run(session, { type: "type", ref: "e7", text: "hi", clear: false, submit: false, keyByKey: false });
    expect(outcome).toMatchObject({ interruption: { code: "REF_STALE" } });
    expect(calls.some(([method]) => method === "Input.insertText")).toBe(false);
  });

  // 目的：页面改写了内置对象时页面函数会抛错；潜在缺陷：CDP 对页面异常不报错，只看返回值就把「抛错」当成「空串 =
  // 成功」——清空失败却按完成上报、字接在旧内容后面，滚动失败却不报到底。
  it("fails the action when a page function raises, and treats a non-string result as a host bug", async () => {
    const raising = fakeSession({ "\"value\" in this": new TypeError("value is read-only") });
    const outcome = await run(raising.session, { type: "type", ref: "e7", text: "hi", clear: true, submit: false, keyByKey: false });
    expect(outcome).toMatchObject({ interruption: { code: "ACTION_FAILED", message: "the page raised an error: TypeError: value is read-only" } });
    expect(raising.calls.some(([method]) => method === "Input.insertText")).toBe(false);

    const stuck = fakeSession({ scrollBy: new Error("blocked") });
    expect(await run(stuck.session, { type: "scroll", direction: "down" })).toMatchObject({ interruption: { code: "ACTION_FAILED" } });

    const broken = fakeSession({ isConnected: true });
    expect(await run(broken.session, { type: "hover", target: { ref: "e7" } })).toMatchObject({
      interruption: { message: "a page function returned boolean instead of a string" },
    });
  });

  // 目的：失败原因报准，模型才拿到对的下一步建议；潜在缺陷：页面停住、超时被一律改写成「元素不可见」，或节点已删除
  // 在不同命令上报成不同的码。
  it("maps a missing node to REF_STALE and only a missing layout to NOT_INTERACTABLE", async () => {
    const code = async (failOn: { method: string; error: Error }) => (await run(fakeSession({}, { failOn }).session, clickOn("e2"))).interruption?.code;
    expect(await code({ method: "DOM.getBoxModel", error: new Error("No node with given id found") })).toBe("REF_STALE");
    expect(await code({ method: "DOM.scrollIntoViewIfNeeded", error: new Error("Session with given id not found.") })).toBe("REF_STALE");
    expect(await code({ method: "DOM.scrollIntoViewIfNeeded", error: new Error("Node does not have a layout object") })).toBe("NOT_INTERACTABLE");
    expect(await code({ method: "DOM.getBoxModel", error: new Error("Could not compute box model.") })).toBe("NOT_INTERACTABLE");
    expect(await code({ method: "DOM.getBoxModel", error: new HostMethodError("PAGE_UNRESPONSIVE", "the page did not respond") })).toBe("PAGE_UNRESPONSIVE");
    expect(await code({ method: "DOM.getBoxModel", error: new HostMethodError("DIALOG_OPEN", "paused") })).toBe("DIALOG_OPEN");
  });

  it("hovers by moving the mouse to the element's centre without clicking", async () => {
    const { session, calls } = fakeSession();
    await run(session, { type: "hover", target: { ref: "e2" } });
    const mouse = calls.filter(([method]) => method === "Input.dispatchMouseEvent").map(([, params]) => params);
    expect(mouse).toEqual([{ type: "mouseMoved", x: 5, y: 10 }]);
  });

  // 目的：右键、双击、带修饰键与坐标落点和真实输入一致；潜在缺陷：双击只发一次 clickCount 2 时页面收不到第一次
  // click，或坐标不经缩放换算就点到别处。
  it("clicks at viewport coordinates with the button, count and modifiers asked for", async () => {
    const { session, calls } = fakeSession({}, { scale: 0.5 });
    await run(session, { type: "click", target: { x: 40, y: 20 }, button: "right", clickCount: 2, modifiers: ["Shift"] });
    const mouse = calls.filter(([method]) => method === "Input.dispatchMouseEvent").map(([, params]) => params);
    expect(mouse).toEqual([
      { type: "mouseMoved", x: 20, y: 10, modifiers: 8 },
      { type: "mousePressed", x: 20, y: 10, button: "right", clickCount: 1, modifiers: 8 },
      { type: "mouseReleased", x: 20, y: 10, button: "right", clickCount: 1, modifiers: 8 },
      { type: "mousePressed", x: 20, y: 10, button: "right", clickCount: 2, modifiers: 8 },
      { type: "mouseReleased", x: 20, y: 10, button: "right", clickCount: 2, modifiers: 8 },
    ]);
    expect(calls.some(([method]) => method === "DOM.resolveNode")).toBe(false);
  });

  // 目的：跨进程 iframe 里的元素能点中、能输入；潜在缺陷：框架内坐标当主视口坐标点到别处，或按节点的命令发到主会话、
  // 找到的是主框架里同号的另一个节点。
  it("routes node commands to the element's frame session and offsets its coordinates", async () => {
    const { session, calls } = fakeSession();
    await run(session, { type: "click", target: { ref: "f3" }, button: "left", clickCount: 1, modifiers: [] });
    expect(calls.find(([method]) => method === "Input.dispatchMouseEvent")).toEqual(["Input.dispatchMouseEvent", { type: "mouseMoved", x: 105, y: 210, modifiers: 0 }]);
    expect(calls.filter(([method]) => method.startsWith("DOM.") || method.startsWith("Runtime.")).every(([, , sessionId]) => sessionId === "S1")).toBe(true);

    const typed = fakeSession();
    await run(typed.session, { type: "type", ref: "f3", text: "hi", clear: false, submit: false, keyByKey: false });
    expect(typed.calls.find(([method]) => method === "DOM.focus")?.[2]).toBe("S1");
    expect(typed.calls.find(([method]) => method === "Input.insertText")?.[2]).toBeUndefined();
  });

  // 目的：用户看得见 Agent 要操作哪里；潜在缺陷：没人看时也去量位置（白白滚动页面、多发命令），高亮把动作已做的
  // 确认再做一遍，或高亮量不到位置时把动作本身弄失败、吞掉动作该报的错。
  it("highlights each action's target before it runs, only when the tab is shown", async () => {
    const shown = fakeSession({}, { shown: true });
    await run(shown.session, clickOn("e2"), { type: "click", target: { x: 50, y: 30 }, button: "left", clickCount: 1, modifiers: [] }, { type: "wait_for", seconds: 0.01 });
    expect(shown.highlighted).toEqual([{ x: 0, y: 0, width: 10, height: 20 }, { x: 38, y: 18, width: 24, height: 24 }]);
    // 元素还在不在页面上只由动作确认一次，高亮不再确认一遍。
    expect(shown.calls.filter(([method, params]) => method === "Runtime.callFunctionOn" && String(params.functionDeclaration).includes("isConnected"))).toHaveLength(2);

    const hidden = fakeSession();
    await run(hidden.session, clickOn("e2"));
    expect(hidden.highlighted).toEqual([]);
    expect(hidden.calls.filter(([method]) => method === "DOM.getBoxModel")).toHaveLength(2);

    const stale = fakeSession({}, { shown: true });
    expect(await run(stale.session, clickOn("e404"))).toMatchObject({ interruption: { code: "REF_STALE" } });
  });

  it("refuses coordinates outside the viewport before touching the page", async () => {
    const { session, calls } = fakeSession();
    const outcome = await run(session, { type: "hover", target: { x: 100, y: 10 } });
    expect(outcome).toMatchObject({ interruption: { code: "OUT_OF_VIEWPORT" } });
    expect(calls).toEqual([]);
  });

  // 目的：滑块与画布要看到按住后的连续移动；潜在缺陷：只发起点与终点，按距离判定拖动开始的页面不响应。
  it("drags with the left button held through intermediate moves", async () => {
    const { session, calls } = fakeSession();
    await run(session, { type: "drag", start: { ref: "e2" }, end: { x: 55, y: 10 } });
    const mouse = calls.filter(([method]) => method === "Input.dispatchMouseEvent").map(([, params]) => params);
    expect(mouse[1]).toEqual({ type: "mousePressed", x: 5, y: 10, button: "left", clickCount: 1 });
    expect(mouse.slice(2, -1).map((event) => [event.type, event.x, event.buttons])).toEqual(
      [15, 25, 35, 45, 55].map((x) => ["mouseMoved", x, 1]),
    );
    expect(mouse.at(-1)).toEqual({ type: "mouseReleased", x: 55, y: 10, button: "left", clickCount: 1 });
    expect(calls.some(([method]) => method === "Input.dispatchDragEvent")).toBe(false);
  });

  // 目的：不在窗口里的标签也能完成 HTML5 拖放；潜在缺陷：系统拖动会话起不来，drop 永远不来；面板缩放显示时拖放事件
  // 按视图坐标发、落到一半处（真实应用验收发现）；拦截在弹窗停住页面后没撤销，用户之后在面板里的拖放被吞掉。
  it("delivers an intercepted HTML5 drag at the end point in CSS pixels and always turns interception off", async () => {
    const { session, calls } = fakeSession({}, { scale: 0.5, dragData: { items: [], dragOperationsMask: 1 }, dialogOn: (method, params) => params.type === "drop" });
    const outcome = await run(session, { type: "drag", start: { x: 0, y: 0 }, end: { x: 30, y: 20 } });
    expect(outcome).toMatchObject({ steps: [{ status: "unknown" }], interruption: { code: "DIALOG_OPEN" } });
    const drags = calls.filter(([method]) => method === "Input.dispatchDragEvent").map(([, params]) => [params.type, params.x, params.y]);
    expect(drags).toEqual([["dragEnter", 30, 20], ["dragOver", 30, 20], ["drop", 30, 20]]);
    expect(calls.findLast(([method]) => method === "Input.dispatchMouseEvent")?.[1]).toMatchObject({ type: "mouseMoved", x: 15, y: 10 });
    expect(calls.filter(([method]) => method === "Input.setInterceptDrags").map(([, params]) => params.enabled)).toEqual([true, false]);
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

  // 目的：按键盘事件工作的控件（自动补全、掩码输入）要收到逐个按键；潜在缺陷：一次写入只触发 input，控件不响应；
  // 中文等键盘上没有的字符被拼成错误的键位。
  it("types key by key with a key event per character", async () => {
    const { session, calls } = fakeSession();
    await run(session, { type: "type", ref: "e7", text: "aB 中\n", clear: false, submit: false, keyByKey: true });
    const downs = calls.filter(([method, params]) => method === "Input.dispatchKeyEvent" && params.type === "keyDown").map(([, params]) => params);
    expect(downs.map(({ key, code, text }) => [key, code, text])).toEqual([
      ["a", "KeyA", "a"], ["B", "KeyB", "B"], [" ", "Space", " "], ["中", undefined, "中"], ["Enter", "Enter", "\r"],
    ]);
    expect(calls.some(([method]) => method === "Input.insertText")).toBe(false);
  });

  // 目的：网站常把文件输入框藏起来、用按钮打开选择框；潜在缺陷：只能填快照里看得见的输入框，或把文件填进别的元素、
  // 把多个文件塞给只收一个的输入框。
  it("uploads to a file input by ref or to the file chooser an action opened", async () => {
    const byRef = fakeSession();
    expect(await run(byRef.session, { type: "upload", paths: ["/w/a.pdf"], ref: "e4" })).toMatchObject({ steps: Array(1).fill({ status: "done" }), interruption: null });
    expect(byRef.calls.at(-1)).toEqual(["DOM.setFileInputFiles", { files: ["/w/a.pdf"], backendNodeId: 4 }]);

    const notInput = fakeSession({ "not a file input": "not a file input" });
    expect(await run(notInput.session, { type: "upload", paths: ["/w/a.pdf"], ref: "e4" })).toMatchObject({ interruption: { code: "NO_FILE_INPUT" } });

    const chosen = fakeSession({}, { fileChooser: { node: { backendNodeId: 9 }, multiple: false } });
    expect(await run(chosen.session, { type: "upload", paths: ["/w/a.pdf"] })).toMatchObject({ steps: Array(1).fill({ status: "done" }), interruption: null });
    expect(chosen.calls.at(-1)).toEqual(["DOM.setFileInputFiles", { files: ["/w/a.pdf"], backendNodeId: 9 }]);
    expect(await run(chosen.session, { type: "upload", paths: ["/w/a.pdf", "/w/b.pdf"] })).toMatchObject({ interruption: { message: expect.stringContaining("single file") } });

    expect(await run(fakeSession().session, { type: "upload", paths: ["/w/a.pdf"] })).toMatchObject({ interruption: { code: "NO_FILE_INPUT" } });
  });

  // 目的：无限滚动到底要停下；潜在缺陷：批次里的滚动空转，模型以为加载了更多。
  it("scrolls the area around a ref and stops with SCROLL_END when nothing moves", async () => {
    const { session, calls } = fakeSession({ "scrollBy": "this area is already at the bottom" });
    const outcome = await run(session, { type: "scroll", direction: "down", ref: "e9" }, clickOn("e1"));
    expect(outcome).toMatchObject({ interruption: { code: "SCROLL_END", message: "this area is already at the bottom" } });
    const scrolled = calls.find(([method, params]) => method === "Runtime.callFunctionOn" && String(params.functionDeclaration).includes("scrollBy"));
    expect(scrolled?.[1].arguments).toEqual([{ value: true }]);

    const page = fakeSession();
    expect(await run(page.session, { type: "scroll", direction: "up" })).toMatchObject({ interruption: null });
    expect(String(page.calls.find(([method]) => method === "Runtime.evaluate")![1].expression)).toContain(".call(document.documentElement, false)");

    const into = fakeSession();
    await run(into.session, { type: "scroll", ref: "e9" });
    expect(into.calls.at(-1)).toEqual(["DOM.scrollIntoViewIfNeeded", { backendNodeId: 9 }]);
  });

  it("waits for text to disappear and reports when it stays", async () => {
    const gone = fakeSession({ innerText: "absent" });
    expect(await run(gone.session, { type: "wait_for", textGone: "Loading", seconds: 1 })).toMatchObject({ interruption: null });

    const stays = fakeSession({ innerText: "found" });
    const outcome = await run(stays.session, { type: "wait_for", textGone: "Loading", seconds: 0.3 });
    expect(outcome.interruption).toMatchObject({ code: "WAIT_TIMEOUT", message: 'the text "Loading" is still on the page after 0.3 seconds' });
  });
});

describe("describeTarget", () => {
  // 目的：工具卡片写清每一步点的是哪个元素；潜在缺陷：只写 e12 这种用户看不懂的编号，或坐标、拖动没有说明。
  it("names elements by their snapshot label, and shows coordinates and drags", () => {
    const label = (ref: string) => (ref === "e2" ? 'button "Add"' : undefined);
    expect(describeTarget({ type: "click", target: { ref: "e2" }, button: "left", clickCount: 1, modifiers: [] }, label)).toBe('button "Add"');
    expect(describeTarget({ type: "type", ref: "e9", text: "x", clear: false, submit: false, keyByKey: false }, label)).toBe("e9");
    expect(describeTarget({ type: "drag", start: { ref: "e2" }, end: { x: 10.4, y: 20.6 } }, label)).toBe('button "Add" → (10, 21)');
    expect(describeTarget({ type: "wait_for", seconds: 1 }, label)).toBeNull();
  });
});

describe("addressToUrl", () => {
  // 目的：地址栏里打本机开发服务、局域网设备的地址能打开；潜在缺陷：`localhost:3000` 被当成名为 `localhost:` 的协议
  // 拦下，或补成 https 连不上只开 http 的服务。
  it("adds http for local network hosts, https elsewhere, and keeps written schemes", () => {
    expect(addressToUrl("localhost:3000/app")).toBe("http://localhost:3000/app");
    expect(addressToUrl("192.168.1.5:5173")).toBe("http://192.168.1.5:5173");
    expect(addressToUrl("printer.local")).toBe("http://printer.local");
    expect(addressToUrl("127.0.0.1:8080")).toBe("http://127.0.0.1:8080");
    expect(addressToUrl("[::1]:5173")).toBe("http://[::1]:5173");
    expect(addressToUrl("example.com/a?b=1")).toBe("https://example.com/a?b=1");
    expect(addressToUrl("localhost.example.com")).toBe("https://localhost.example.com");
    expect(addressToUrl("http://example.com")).toBe("http://example.com");
    // 写了别的协议的照原样交给协议校验去拦。
    expect(addressToUrl("file:///etc/passwd")).toBe("file:///etc/passwd");
    expect(addressToUrl("javascript:alert(1)")).toBe("javascript:alert(1)");
  });

  // 目的：地址栏里粘贴本机路径能打开；潜在缺陷：`/Users/x/测试.html` 被补成 `https:///Users/…`，主机名变成 `users`。
  it("turns absolute local paths into file URLs", () => {
    expect(addressToUrl("/Users/x/测试 结果/a.html")).toBe("file:///Users/x/%E6%B5%8B%E8%AF%95%20%E7%BB%93%E6%9E%9C/a.html");
    expect(addressToUrl("~/a.html")).toBe(`file://${os.homedir()}/a.html`);
  });
});

describe("isPageUrl / pageUrl", () => {
  // 目的：页面自身的跳转与弹窗也能在本机页面之间进行；潜在缺陷：本机 HTML 里的相对链接被当成非网页拦下，或放进拉起
  // 别的应用的地址。
  it("accepts web pages and local files only", () => {
    for (const url of ["https://a.b/", "http://localhost:3000", "file:///tmp/b.html"]) expect(isPageUrl(url)).toBe(true);
    for (const url of ["javascript:alert(1)", "chrome://settings", "mailto:a@b.c", "vscode://x", "not a url"]) expect(isPageUrl(url)).toBe(false);
    expect(codeOf(() => pageUrl("chrome://settings"))).toBe("NAVIGATION_BLOCKED");
  });
});

