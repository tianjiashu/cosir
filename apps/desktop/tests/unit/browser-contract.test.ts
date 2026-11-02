import { describe, expect, it } from "vitest";

import samples from "@/electron/main/browser/contract/wire-samples.json";
import schemas from "@/electron/main/browser/contract/tool-args.schema.json";
import { isSupportedKey } from "@/electron/main/browser/input/keyboard";
import { isSupportedModifier } from "@/electron/main/browser/input/modifiers";
import { parseAct, parseEvaluate, parseLogs, parseNavigate, parseScreenshot, parseSnapshot, parseTabs } from "@/electron/main/browser/params";
import { actWire, evaluateWire, logsWire, pageWire, screenshotWire, tabsWire } from "@/electron/main/browser/wire";
import type { ActOutcome, EvaluateResult, LogsResult, PageHead, PageResult } from "@/electron/main/browser/browser-host";

/**
 * 宿主与后端之间的线上契约：宿主这边断言 `wire.ts` 的输出逐字等于 `contract/wire-samples.json`，后端守卫测试
 * （`apps/backend/tests/test_browser_contract_guard.py`）用同一份样例渲染正文——任一边改了字段，另一边的测试就失败。
 */

const head = (status: number | null, emulation: PageHead["emulation"]): PageHead => ({
  tabId: "t3", url: "http://127.0.0.1:5173/login", title: "Sign in", loading: false, status, emulation,
});
const defaultEmulation = { width: 1280, height: 800, mobile: false, colorScheme: null };

describe("wire samples", () => {
  it("page results", () => {
    const page: PageResult = {
      ...head(404, { width: 390, height: 844, mobile: true, colorScheme: "dark" }),
      snapshot: "- heading \"Sign in\" [level=1]\n- button \"Go\" [ref=e2]",
      frames: 1,
      notes: ["the page tried to open a file chooser; file uploads are not supported, so it was blocked"],
      newLogs: { consoleErrors: 2, consoleWarnings: 1, failedRequests: 1 },
      otherTabs: ["t1"],
      dialog: null,
    };
    expect(pageWire(page)).toEqual(samples.page);
    const prompt: PageResult = {
      ...head(404, defaultEmulation),
      snapshot: "",
      frames: 0,
      notes: [],
      newLogs: { consoleErrors: 0, consoleWarnings: 0, failedRequests: 0 },
      otherTabs: [],
      dialog: { id: 9, type: "prompt", message: "Your name?", defaultText: "Ada" },
    };
    expect(pageWire(prompt)).toEqual(samples.page_prompt);
  });

  it("act outcomes with a dialog", () => {
    const outcome: ActOutcome = {
      ...head(200, defaultEmulation),
      snapshot: "",
      frames: 0,
      notes: [],
      newLogs: { consoleErrors: 0, consoleWarnings: 0, failedRequests: 0 },
      otherTabs: [],
      dialog: { id: 7, type: "confirm", message: "Delete it?", defaultText: "" },
      completed: 1,
      failure: { index: 1, code: "DIALOG_OPEN", message: "the page is paused: it is showing a confirm dialog (#7)" },
      targets: ['button "Delete"', null],
    };
    expect(actWire(outcome)).toEqual(samples.act);
  });

  it("console and network logs", () => {
    const consoleLogs: LogsResult = {
      ...head(200, defaultEmulation),
      kind: "console",
      entries: [
        { seq: 12, level: "error", text: "Uncaught TypeError: x is undefined", source: "http://127.0.0.1:5173/src/main.ts:42", frame: null },
        { seq: 15, level: "warning", text: "deprecated API", source: null, frame: "http://localhost:6006/embed" },
      ],
      next: 16,
      moreBefore: true,
      moreAfter: false,
      droppedBefore: null,
    };
    const networkLogs: LogsResult = {
      ...head(200, defaultEmulation),
      kind: "network",
      entries: [
        { seq: 20, method: "GET", url: "http://127.0.0.1:5173/api/me", status: 401, resourceType: "Fetch", failure: null, durationMs: 31 },
        { seq: 21, method: "POST", url: "http://127.0.0.1:5173/api/login", status: null, resourceType: "XHR", failure: "net::ERR_CONNECTION_REFUSED", durationMs: null },
        { seq: 22, method: "GET", url: "http://127.0.0.1:5173/api/stream", status: null, resourceType: "Fetch", failure: null, durationMs: null },
      ],
      next: 23,
      moreBefore: false,
      moreAfter: true,
      droppedBefore: 18,
    };
    expect(logsWire(consoleLogs)).toEqual(samples.console_logs);
    expect(logsWire(networkLogs)).toEqual(samples.network_logs);
  });

  it("evaluate results", () => {
    const ok: EvaluateResult = { ...head(200, defaultEmulation), value: "{\"user\":null,\"items\":3}", truncated: false, error: null, dialog: null };
    const failed: EvaluateResult = { ...head(200, defaultEmulation), value: null, truncated: false, error: "ReferenceError: foo is not defined", dialog: null };
    expect(evaluateWire(ok)).toEqual(samples.evaluate);
    expect(evaluateWire(failed)).toEqual(samples.evaluate_error);
    const base = { ...head(200, defaultEmulation), error: null };
    expect(evaluateWire({ ...base, value: "undefined", truncated: false, dialog: null })).toEqual(samples.evaluate_undefined);
    expect(evaluateWire({ ...base, value: "[1,2,3", truncated: true, dialog: null })).toEqual(samples.evaluate_truncated);
    expect(evaluateWire({ ...base, value: null, truncated: false, dialog: { id: 4, type: "confirm", message: "Proceed?", defaultText: "" } }))
      .toEqual(samples.evaluate_dialog);
  });

  it("screenshots and tab lists", () => {
    expect(screenshotWire({ ...head(200, defaultEmulation), image: "data:image/jpeg;base64,/9j/AAAA" })).toEqual(samples.screenshot);
    expect(tabsWire([
      { tabId: "t1", title: "Home", url: "http://127.0.0.1:5173/", current: false, createdBy: "user" },
      { tabId: "t3", title: "Sign in", url: "http://127.0.0.1:5173/login", current: true, createdBy: "agent" },
    ])).toEqual(samples.tabs);
  });
});

describe("exported argument schema", () => {
  // 目的：按键与修饰键的词表只有后端一份；潜在缺陷：后端放开了一个键，宿主没有实现，动作执行时才报错。
  it("only names keys and modifiers the host can press", () => {
    const press = schemas.browser_act.$defs.PressAction.properties;
    const named = press.key.anyOf[0].enum!;
    expect(named.filter((key) => !isSupportedKey(key))).toEqual([]);
    expect(press.key.anyOf[1].pattern).toBe("^[a-z0-9]$");
    expect(["a", "z", "0", "9"].every(isSupportedKey)).toBe(true);
    const modifiers = press.modifiers.anyOf[0].items!.enum;
    expect(modifiers.filter((modifier) => !isSupportedModifier(modifier))).toEqual([]);
  });
});

describe("argument keys the host reads", () => {
  // 目的：宿主按字面键名读参数；潜在缺陷：后端改了字段名，schema 与 ajv 跟着变，宿主却读到 undefined 后静默用默认值。
  // 每个工具给一份所有字段都填了非默认值的样例：键集合必须等于 schema 的属性，解析结果里必须能找到每个值。
  const host = { task_id: 7, limits: { per_task: 5, total: 20 }, download_dir: "/ws/.cosir/Download" };
  const cases: [keyof typeof schemas, (params: unknown) => unknown, Record<string, unknown>][] = [
    ["browser_navigate", parseNavigate, { url: "https://example.com/z", action: null, tab: "t9", new_tab: true, viewport: { width: 391, height: 845, mobile: true }, color_scheme: "dark" }],
    ["browser_snapshot", parseSnapshot, { tab: "t9", scope: "viewport", find: "needle", from_line: 37 }],
    ["browser_screenshot", parseScreenshot, { tab: "t9", full_page: false, ref: "e4" }],
    ["browser_tabs", parseTabs, { tab: "t9", action: "close" }],
    ["browser_logs", parseLogs, { kind: "console", since: 41, level: "warning", failed_only: false, pattern: "needle", limit: 7, tab: "t9" }],
    ["browser_logs", parseLogs, { kind: "network", since: 41, level: null, failed_only: true, pattern: "needle", limit: 7, tab: "t9" }],
    ["browser_evaluate", parseEvaluate, { tab: "t9", expression: "document.title" }],
  ];
  it.each(cases)("%s", (tool, parse, args) => {
    expect(Object.keys(args).sort()).toEqual(Object.keys(schemas[tool].properties).sort());
    const parsed = JSON.stringify(parse({ ...host, ...args }));
    const values = Object.values(args).flatMap((value) => (value && typeof value === "object" ? Object.values(value) : [value]));
    for (const value of values) {
      // null 是「未给」；false 是部分开关的默认值，只看它们不会被误读成别的。
      if (value === null || value === false) continue;
      expect(parsed).toContain(JSON.stringify(value));
    }
  });

  it("browser_act", () => {
    const actions = {
      click: { type: "click", ref: null, x: 4, y: 8, button: "right", click_count: 2, modifiers: ["Alt"] },
      hover: { type: "hover", ref: "e6", x: null, y: null },
      drag: { type: "drag", start: { ref: "e7", x: null, y: null }, end: { ref: null, x: 1, y: 2 } },
      type: { type: "type", ref: "e1", text: "hello", clear: true, submit: true, key_by_key: true },
      press: { type: "press", key: "Enter", modifiers: ["Shift"], ref: "e2" },
      scroll: { type: "scroll", direction: "up", ref: "e3" },
      wait_for: { type: "wait_for", text: null, text_gone: "Loading", seconds: 3 },
      dialog: { type: "dialog", accept: true, text: "Ada", id: 4 },
      select: { type: "select", ref: "e5", values: ["b"] },
      upload: { type: "upload", paths: ["/w/a.pdf"], ref: "e8" },
    };
    const parsed = parseAct({ ...host, tab: "t9", actions: Object.values(actions) }).actions;
    expect(parsed).toEqual([
      { type: "click", target: { x: 4, y: 8 }, button: "right", clickCount: 2, modifiers: ["Alt"] },
      { type: "hover", target: { ref: "e6" } },
      { type: "drag", start: { ref: "e7" }, end: { x: 1, y: 2 } },
      { type: "type", ref: "e1", text: "hello", clear: true, submit: true, keyByKey: true },
      { type: "press", key: "Enter", modifiers: ["Shift"], ref: "e2" },
      { type: "scroll", direction: "up", ref: "e3" },
      { type: "wait_for", text: undefined, textGone: "Loading", seconds: 3 },
      { type: "dialog", accept: true, text: "Ada", id: 4 },
      { type: "select", ref: "e5", values: ["b"] },
      { type: "upload", paths: ["/w/a.pdf"], ref: "e8" },
    ]);
    // 动作类型的全集也要对上：后端新增一种动作而宿主没有对应分支时，在这里而不是运行时暴露。
    type ActionDef = { properties: Record<string, unknown> & { type?: { const?: string; enum?: string[] } } };
    const defs = Object.values(schemas.browser_act.$defs as Record<string, ActionDef>).filter((def) => def.properties?.type);
    const byType = Object.fromEntries(defs.map((def) => [def.properties.type!.const ?? def.properties.type!.enum![0], def]));
    expect(Object.keys(byType).sort()).toEqual(Object.keys(actions).sort());
    for (const [type, sample] of Object.entries(actions)) {
      expect(Object.keys(sample).sort(), type).toEqual(Object.keys(byType[type].properties).sort());
    }
  });
});
