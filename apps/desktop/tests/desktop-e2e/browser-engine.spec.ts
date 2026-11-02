import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import type { AddressInfo } from "node:net";
import { _electron as electron, expect, test, type ElectronApplication } from "@playwright/test";

/** 与 `global-setup.mjs` 的打包位置一致。 */
const browserHarnessBundle = path.resolve(__dirname, "..", "..", "..", "..", "target", "electron-browser-harness", "main.cjs");

/**
 * 内置浏览器引擎在真实 Electron 里的行为（不经后端与链路）：快照、导航失败、JS 弹窗作答、非网页地址拦截、
 * 渲染进程崩溃恢复、标签归属与回收、UA、页面开出的标签（opener）、下载落盘、缩放显示下的点击。
 * 链路往返由 P3-1 的两端单测覆盖，工具由后端单测覆盖；用户在面板里选文件要点系统对话框，不在这里覆盖。
 * 注意：Playwright 接管每个网页时会开焦点模拟，后台标签缺了焦点模拟收不到合成点击这类问题在这里测不出
 * （由 browser-initialization 单测守住）。
 */

const PAGES: Record<string, string> = {
  "/form": `<title>Form</title><h1>Sign up</h1><p>Fill in the form.</p><label>Email <input name="email" value="a@b.c"></label><button disabled>Send</button><a href="/second">Next</a>`,
  "/second": `<title>Second</title><p>Second page</p>`,
  "/dialog": `<title>Dialog</title><script>setTimeout(() => { document.title = "answered:" + confirm("Continue?"); }, 0)</script>`,
  "/prompt": `<title>Prompt</title><script>setTimeout(() => { document.title = "name:" + prompt("Your name?", "x"); }, 0)</script>`,
  "/delete": `<title>Delete</title><button onclick="document.title = 'deleted:' + confirm('Delete it?')">Delete</button>`,
  "/opener": `<title>Opener</title><p id="s">waiting</p><button onclick="window.open('/child', 'login', 'width=400,height=300')">Sign in</button>
    <script>window.addEventListener("message", (e) => { document.title = "got:" + e.data; });</script>`,
  "/child": `<title>Child</title><script>setTimeout(() => { window.opener.postMessage("token-123", "*"); setTimeout(() => window.close(), 200); }, 200)</script>`,
  "/download": `<title>Download</title><a href="/report.csv" download>Export</a>`,
  "/popup-alert": `<title>Popup alert</title><button onclick="window.open('/alert-on-load')">Open</button>`,
  "/alert-on-load": `<title>Alerting</title><script>alert("Sign-in failed"); document.title = "after alert";</script>`,
  "/popup-confirm": `<title>Popup confirm</title><button onclick="window.open('/confirm-later')">Open</button>`,
  "/confirm-later": `<title>Confirm later</title><script>setTimeout(() => { document.title = "c:" + confirm("Allow?"); }, 300)</script>`,
  "/popup-download": `<title>Popup download</title><button onclick="window.open('/download-and-close')">Export</button>`,
  "/download-and-close": `<title>Exporting</title><script>location.href = "/report.csv"; setTimeout(() => window.close(), 300)</script>`,
  "/two-dialogs": `<title>Two</title><script>setTimeout(() => { alert("first"); document.title = "second:" + confirm("second?"); }, 0)</script>`,
  "/bounce": `<title>Bounce</title><script>location.replace("/responsive")</script>`,
  "/responsive": `<title>Responsive</title><meta name="viewport" content="width=device-width, initial-scale=1"><p>Hi</p>`,
  "/noisy": `<title>Noisy</title><script>
    console.log("hello log"); console.warn("careful now"); console.error("bad thing");
    setTimeout(() => { throw new Error("uncaught boom"); }, 0);
    fetch("/api/missing").catch(() => undefined);
  </script>`,
  "/clickable": `<title>Clickable</title><div style="cursor: pointer" onclick="document.title = 'card'"><span>Open card</span> <span style="cursor: pointer">details</span></div>
    <a href="/second?q=1">Next</a> <a href="https://example.com/x">External</a>`,
  "/pointer": `<title>Pointer</title><body style="margin: 0">
    <div style="position: absolute; left: 0; top: 0; width: 400px; height: 200px"
      oncontextmenu="event.preventDefault(); document.title = 'menu ' + event.clientX + ',' + event.clientY"
      ondblclick="document.title = 'double ' + event.shiftKey">pad</div>
    <div draggable="true" style="position: absolute; left: 0; top: 300px; width: 100px; height: 40px"
      ondragstart="event.dataTransfer.setData('text', 'a')">Item A</div>
    <div style="position: absolute; left: 300px; top: 300px; width: 100px; height: 40px"
      ondragover="event.preventDefault()" ondrop="document.title = 'dropped ' + event.dataTransfer.getData('text')">Drop here</div></body>`,
  "/keys": `<title>Keys</title><input aria-label="Code" onkeyup="window.keys = (window.keys || 0) + 1; document.title = window.keys + ' ' + this.value">`,
  "/frames": `<title>Frames</title><body style="margin: 0"><div style="height: 900px">Top of the page</div>
    <iframe src="/frame-same" title="Same"></iframe>
    <iframe id="cross" title="Cross" style="width: 400px; height: 200px"></iframe>
    <script>
      document.getElementById("cross").src = "http://localhost:" + location.port + "/frame-cross";
      addEventListener("message", (event) => { document.title = "got " + event.data; });
    </script></body>`,
  "/frame-same": `<button onclick="parent.document.title = 'same clicked'">In same</button>`,
  "/frame-cross": `<body style="margin: 20px"><input aria-label="Note"> <button onclick="parent.postMessage(document.querySelector('input').value, '*')">Send</button>
    <input type="file" aria-label="Attach" onchange="parent.postMessage('file ' + this.files[0].name, '*')"></body>`,
  "/viewport": `<title>Viewport</title><body style="margin:0">
    <header style="position:fixed;top:0;left:0;right:0;height:40px;background:#eee"><button>Fixed menu</button></header>
    ${Array.from({ length: 60 }, (_, i) => `<div style="height:60px;padding-top:40px"><button>Row ${i + 1}</button></div>`).join("")}
    <iframe src="/frame-same" title="Bottom frame"></iframe></body>`,
  "/counter": `<title>Counter</title><h1>Shop</h1>${Array.from({ length: 12 }, (_, i) => `<p>Item ${i + 1}</p>`).join("")}
    <button onclick="document.getElementById('n').textContent = Number(document.getElementById('n').textContent) + 1">Add</button>
    <span id="n">0</span> <a href="/clickable">Elsewhere</a>`,
  "/wide": `<title>Wide</title><body style="margin:0;width:980px"><button style="position:absolute;left:600px;top:300px;width:200px;height:100px" onclick="document.title = 'hit'">Far button</button></body>`,
  "/far": `<title>Far</title><button style="position: absolute; left: 1100px; top: 700px" onclick="document.title = 'far clicked'">Far</button>`,
  "/escape": `<title>Escape</title><script>setTimeout(() => { location.href = "file:///etc/hosts"; }, 0)</script>`,
  "/ua": `<script>document.title = navigator.userAgent</script>`,
  "/act": `<title>Act</title>
    <form action="/done" method="get"><label>Query <input name="q"></label><button type="submit">Search</button></form>
    <label>Color <select name="color"><option value="r">Red</option><option value="b">Blue</option></select></label>
    <button onclick="setTimeout(() => { document.getElementById('out').textContent = 'loaded later'; }, 400)">Load</button>
    <p id="out"></p>
    <input type="file" aria-label="Upload">
    <a href="/second" target="_blank">Popup</a>
    <script>document.querySelector('select').addEventListener('change', (e) => { document.title = 'color:' + e.target.value; });</script>`,
  "/done": `<title>Done</title><p>Search finished</p>`,
  "/rich": `<title>Rich</title>
    <button onmouseenter="document.getElementById('items').hidden = false">Menu</button>
    <div id="items" hidden><a href="#picked">Hidden item</a></div>
    <div style="height: 100px; overflow: auto"><div style="height: 150px"><button>Inside</button></div></div>
    <p id="loading">Loading</p>
    <script>
      setTimeout(() => document.getElementById("loading").remove(), 300);
      document.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "k") document.title = "shortcut"; });
    </script>
    ${Array.from({ length: 800 }, (_, i) => `<a href="#i${i}">Item ${i}</a><br>`).join("")}`,
};

const roomy = { perTask: 5, total: 50 };

let server: http.Server;
let origin: string;
let app: ElectronApplication;

const LIMITS = { perTask: 2, total: 3 };
const DOWNLOAD_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "cosir-browser-downloads-"));
const context = (limits: { perTask: number; total: number }) => ({ limits, downloadDir: DOWNLOAD_DIR });

test.beforeAll(async () => {
  server = http.createServer((request, response) => {
    if (request.url === "/report.csv") {
      response.writeHead(200, { "content-type": "text/csv", "content-disposition": "attachment; filename=report.csv" });
      response.end("a,b\n1,2\n");
      return;
    }
    if (request.url === "/moved") {
      response.writeHead(302, { location: "/responsive" });
      response.end();
      return;
    }
    if (request.url === "/no-content") {
      response.writeHead(204);
      response.end();
      return;
    }
    const body = PAGES[(request.url ?? "").split("?")[0]];
    response.writeHead(body ? 200 : 404, { "content-type": "text/html; charset=utf-8" });
    response.end(body ?? "not found");
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  app = await electron.launch({
    args: [browserHarnessBundle],
    env: { ...process.env, CODING_AGENT_DATA_DIR: fs.mkdtempSync(path.join(os.tmpdir(), "cosir-browser-e2e-")) } as Record<string, string>,
  });
  await expect.poll(() => app.evaluate(() => Boolean((globalThis as { browserHarness?: unknown }).browserHarness))).toBe(true);
});

test.afterAll(async () => {
  await app?.close();
  server?.close();
});

type Page = {
  tabId: string; url: string; title: string; snapshot: string; notes: string[]; otherTabs: string[];
  dialog: { id: number; type: string; message: string; defaultText: string } | null;
};
type Outcome = { ok: true; value: Page } | { ok: false; code: string; message: string };

/** 宿主内部形状的左键单击（`call` 直接调用 `BrowserHost`，不经线上适配）。 */
const clickOn = (ref: string | undefined) => ({ type: "click", target: { ref }, button: "left", clickCount: 1, modifiers: [] });

/** 断言调用成功并取出页面结果。 */
function pageOf(outcome: Outcome): Page {
  if (!outcome.ok) throw new Error(`expected success, got ${outcome.code}: ${outcome.message}`);
  return outcome.value;
}

/** 在 main 进程里调用 `BrowserHost` 的方法，把 `HostMethodError` 转成可序列化的结果。 */
function call(method: "navigate" | "snapshot" | "act" | "screenshot" | "manageTabs", request: Record<string, unknown>): Promise<Outcome> {
  return app.evaluate(async (_electron, { method, request, downloadDir }) => {
    const { host } = (globalThis as unknown as { browserHarness: { host: Record<string, (...args: unknown[]) => Promise<unknown>> } }).browserHarness;
    try {
      // 每个请求都带标签上下文（宿主据此刷新上限）；用例没指定时给宽松的上限。
      const withContext = { context: { limits: { perTask: 5, total: 50 }, downloadDir }, ...request };
      return { ok: true as const, value: await host[method](withContext, new AbortController().signal) as unknown as Page };
    } catch (error) {
      const failure = error as { code?: string; message?: string };
      return { ok: false as const, code: failure.code ?? "UNKNOWN", message: failure.message ?? String(error) };
    }
  }, { method, request, downloadDir: DOWNLOAD_DIR });
}

type LinkOutcome = { ok: true; value: Record<string, unknown> } | { ok: false; code: string; message: string };

/** 后端照参数模型的 `model_dump()` 发送：每个字段都在，未填的为 null。这里按方法与动作类型补齐。 */
const WIRE_DEFAULTS: Record<string, Record<string, unknown>> = {
  "browser.navigate": { url: null, action: null, tab: null, new_tab: false, viewport: null, color_scheme: null },
  "browser.snapshot": { tab: null, scope: "page", find: null, from_line: null },
  "browser.act": { tab: null },
  "browser.logs": { since: null, level: null, failed_only: false, pattern: null, limit: 50, tab: null },
  "browser.evaluate": { tab: null },
  "browser.screenshot": { tab: null, full_page: false, ref: null },
  "browser.tabs": { tab: null },
};
const POINT_DEFAULTS = { ref: null, x: null, y: null };
const ACTION_DEFAULTS: Record<string, Record<string, unknown>> = {
  upload: { ref: null },
  type: { clear: false, submit: false, key_by_key: false },
  dialog: { text: null, id: null },
  click: { ...POINT_DEFAULTS, button: "left", click_count: 1, modifiers: null },
  hover: POINT_DEFAULTS,
};

function wireParams(method: string, params: Record<string, unknown>): Record<string, unknown> {
  const filled = { ...WIRE_DEFAULTS[method], ...params };
  if (Array.isArray(filled.actions)) {
    filled.actions = (filled.actions as Record<string, unknown>[]).map((action) => {
      const wire = { ...ACTION_DEFAULTS[action.type as string], ...action };
      if (action.type === "drag") Object.assign(wire, { start: { ...POINT_DEFAULTS, ...wire.start as object }, end: { ...POINT_DEFAULTS, ...wire.end as object } });
      return wire;
    });
  }
  if (filled.viewport && typeof filled.viewport === "object") filled.viewport = { mobile: false, ...filled.viewport };
  return filled;
}

/** 按后端发来的线上形状调用方法（经注册表、schema 校验与 `wire.ts`），覆盖链路两端的适配层。 */
function link(method: string, taskId: number, params: Record<string, unknown>): Promise<LinkOutcome> {
  params = wireParams(method, params);
  return app.evaluate(async (_electron, { method, taskId, params, downloadDir }) => {
    const { invoke } = (globalThis as unknown as { browserHarness: { invoke: (method: string, params: unknown) => Promise<unknown> } }).browserHarness;
    try {
      const value = await invoke(method, { task_id: taskId, limits: { per_task: 5, total: 50 }, download_dir: downloadDir, ...params });
      return { ok: true as const, value: value as Record<string, unknown> };
    } catch (error) {
      const failure = error as { code?: string; message?: string };
      return { ok: false as const, code: failure.code ?? "UNKNOWN", message: failure.message ?? String(error) };
    }
  }, { method, taskId, params, downloadDir: DOWNLOAD_DIR });
}

function valueOf(outcome: LinkOutcome): Record<string, unknown> {
  if (!outcome.ok) throw new Error(`expected success, got ${outcome.code}: ${outcome.message}`);
  return outcome.value;
}

const navigate = (taskId: number, target: Record<string, unknown>, tab?: string) => call("navigate", { taskId, tab, target, context: context(LIMITS) });

test("opens a tab on first navigation and returns a snapshot with refs", async () => {
  const page = pageOf(await navigate(1, { url: `${origin}/form` }));
  expect(page).toMatchObject({ tabId: "t1", title: "Form", otherTabs: [], notes: [] });
  expect(page.snapshot).toContain(`heading "Sign up" [level=1]`);
  expect(page.snapshot).toMatch(/textbox "Email" \[ref=e\d+\] value="a@b.c"/);
  expect(page.snapshot).toMatch(/button "Send" \[ref=e\d+\] \[disabled\]/);
  expect(page.snapshot).toMatch(/link "Next" \[ref=e\d+\]/);
});

test("supports history navigation within the task's current tab", async () => {
  await navigate(1, { url: `${origin}/second` });
  const back = await navigate(1, { action: "back" });
  expect(back).toMatchObject({ ok: true, value: { tabId: "t1", title: "Form" } });
  const reload = await navigate(1, { action: "reload" });
  expect(reload).toMatchObject({ ok: true, value: { title: "Form" } });
});

test("reports load failures with the Chromium error", async () => {
  const outcome = await navigate(1, { url: "http://127.0.0.1:9/" });
  expect(outcome).toMatchObject({ ok: false, code: "NAVIGATION_FAILED" });
  expect(outcome.ok ? "" : outcome.message).toContain("ERR_UNSAFE_PORT");
});

// 目的：弹窗交给人或 Agent 作答；潜在缺陷：confirm() 被自动取消（用户无法确认删除），或页面停住后 CDP 挂到超时。
test("pauses on JavaScript dialogs until the agent or the user answers", async () => {
  const opened = pageOf(await navigate(1, { url: `${origin}/dialog` }));
  const shown = opened.dialog ?? pageOf(await call("snapshot", { taskId: 1 })).dialog;
  expect(shown).toMatchObject({ type: "confirm", message: "Continue?", defaultText: "" });
  expect(await call("screenshot", { taskId: 1, fullPage: false })).toMatchObject({ ok: false, code: "DIALOG_OPEN" });
  const blocked = pageOf(await call("act", { taskId: 1, actions: [{ type: "press", key: "Tab" }] })) as ActPage;
  expect(blocked).toMatchObject({ completed: 0, failure: { code: "DIALOG_OPEN" } });
  // 调用开始时已有的弹窗必须按编号作答。
  const unnamed = pageOf(await call("act", { taskId: 1, actions: [{ type: "dialog", accept: true }] })) as ActPage;
  expect(unnamed).toMatchObject({ completed: 0, failure: { code: "DIALOG_MISMATCH" } });
  const answered = pageOf(await call("act", { taskId: 1, actions: [{ type: "dialog", accept: true, id: shown!.id }] })) as ActPage;
  expect(answered).toMatchObject({ completed: 1, failure: null, dialog: null, title: "answered:true" });

  await navigate(1, { url: `${origin}/prompt` });
  await expect.poll(async () => pageOf(await call("snapshot", { taskId: 1 })).dialog?.type).toBe("prompt");
  const summary = await app.evaluate(() => {
    type Harness = { host: { state(): { tabs: { dialog: { id: number } }[] }; userAnswerDialog(tab: string, id: number, answer: unknown): void } };
    const { host } = (globalThis as unknown as { browserHarness: Harness }).browserHarness;
    const dialog = host.state().tabs[0].dialog;
    host.userAnswerDialog("t1", dialog.id, { accept: true, text: "Ada" });
    return dialog;
  });
  expect(summary).toMatchObject({ type: "prompt", message: "Your name?", defaultText: "x" });
  await expect.poll(async () => pageOf(await call("snapshot", { taskId: 1 })).title).toBe("name:Ada");
});

// 目的：「点删除 + 确认」一次完成；潜在缺陷：弹窗让点击被误判失败，或后续动作挂到 CDP 超时。
test("answers a dialog opened by an action in the same batch", async () => {
  const page = pageOf(await navigate(1, { url: `${origin}/delete` }));
  const button = refOf(page.snapshot, "button", "Delete");
  const outcome = pageOf(await call("act", { taskId: 1, actions: [clickOn(button), { type: "dialog", accept: false }] })) as ActPage;
  expect(outcome).toMatchObject({ completed: 2, failure: null, title: "deleted:false" });
});

// 目的：页面不能把标签带离网页；潜在缺陷：经脚本跳转读本机文件。
// 网页跳 file: 由 Chromium 自己拦下（不触发 will-navigate），引擎的协议拦截兜住其余非网页协议。
test("keeps scripted navigation to local files from leaving the page", async () => {
  await navigate(1, { url: `${origin}/escape` });
  await new Promise((resolve) => setTimeout(resolve, 500));
  expect(await call("snapshot", { taskId: 1 })).toMatchObject({ ok: true, value: { url: `${origin}/escape`, title: "Escape" } });
});

test("recovers from a renderer crash by navigating again", async () => {
  await navigate(1, { url: `${origin}/form` });
  await app.evaluate(() => {
    const { host } = (globalThis as unknown as { browserHarness: { host: { tabs: Map<string, { tab: { view: Electron.WebContentsView } }> } } }).browserHarness;
    host.tabs.get("t1")!.tab.view.webContents.forcefullyCrashRenderer();
  });
  await expect.poll(async () => {
    const outcome = await call("snapshot", { taskId: 1 });
    return outcome.ok ? "ok" : outcome.code;
  }).toBe("PAGE_CRASHED");
  expect(await navigate(1, { url: `${origin}/second` })).toMatchObject({ ok: true, value: { title: "Second" } });
});

// 目的：标签归属是硬边界、上限生效、删除后回收；潜在缺陷：子 Agent 操作父 Agent 的页面或标签无限增长。
test("keeps tabs per task, enforces limits and drops a task's tabs", async () => {
  const taskOneTab = pageOf(await navigate(1, { url: `${origin}/second` })).tabId;
  expect(await navigate(2, { url: `${origin}/second` })).toMatchObject({ ok: true });
  expect(await call("snapshot", { taskId: 2, tab: taskOneTab })).toMatchObject({ ok: false, code: "TAB_NOT_FOUND" });
  expect(await navigate(3, { url: `${origin}/second` })).toMatchObject({ ok: true });
  expect(await navigate(4, { url: `${origin}/second` })).toMatchObject({ ok: false, code: "TAB_LIMIT" });

  await app.evaluate(() => {
    (globalThis as unknown as { browserHarness: { host: { dropTasks(ids: number[]): void } } }).browserHarness.host.dropTasks([2, 3]);
  });
  expect(await call("snapshot", { taskId: 2 })).toMatchObject({ ok: false, code: "TAB_NOT_FOUND" });
  expect(await navigate(4, { url: `${origin}/second` })).toMatchObject({ ok: true });
});

test("presents a standard Chrome user agent", async () => {
  const title = pageOf(await navigate(5, { url: `${origin}/ua` })).title;
  expect(title).toContain("Chrome/");
  expect(title).not.toContain("Electron");
});

// 目的：工作台同一时刻只挂一个原生画面、摘下后回到后台视口；潜在缺陷：切标签后旧画面残留在窗口上盖住页面。
test("places one tab at a time on the window and freezes a frame", async () => {
  const first = pageOf(await call("navigate", { taskId: 6, target: { url: `${origin}/form` }, context: context(roomy) })).tabId;
  const second = pageOf(await call("navigate", { taskId: 7, target: { url: `${origin}/second` }, context: context(roomy) })).tabId;

  const placement = await app.evaluate((_electron, { first, second }) => {
    type Harness = {
      host: { place(tabId: string, rect: unknown): void; freeze(tabId: string): Promise<string>; tabs: Map<string, { tab: { view: Electron.WebContentsView } }> };
      window: Electron.BrowserWindow;
    };
    const { host, window } = (globalThis as unknown as { browserHarness: Harness }).browserHarness;
    const base = window.contentView.children.length;
    const rect = { x: 10, y: 20, width: 300, height: 200 };
    host.place(first, rect);
    const withFirst = window.contentView.children.length;
    host.place(second, rect);
    const withSecond = window.contentView.children.length;
    const firstAttached = window.contentView.children.includes(host.tabs.get(first)!.tab.view);
    const placed = host.tabs.get(second)!.tab.view.getBounds();
    host.place(second, null);
    return {
      base,
      withFirst,
      withSecond,
      firstAttached,
      placed,
      after: window.contentView.children.length,
      detached: host.tabs.get(second)!.tab.view.getBounds(),
    };
  }, { first, second });

  expect(placement.withFirst).toBe(placement.base + 1);
  expect(placement.withSecond).toBe(placement.base + 1);
  expect(placement.firstAttached).toBe(false);
  // 300×200 的占位区域里按 1280×800 等比缩放、水平居中、顶部对齐：300×187.5，下方留白。
  expect(placement.placed).toEqual({ x: 10, y: 20, width: 300, height: 188 });
  expect(placement.after).toBe(placement.base);
  expect(placement.detached).toEqual({ x: 0, y: 0, width: 1280, height: 800 });

  const frame = await app.evaluate((_electron, { second }) => {
    const { host } = (globalThis as unknown as { browserHarness: { host: { freeze(tabId: string): Promise<string> } } }).browserHarness;
    return host.freeze(second);
  }, { second });
  expect(frame).toMatch(/^data:image\/jpeg;base64,.{100,}/);
});

/** 按可访问名称从快照里取 ref。 */
function refOf(snapshot: string, role: string, name: string): string {
  const match = snapshot.match(new RegExp(`${role} "${name}"[^\\n]*\\[ref=(e\\d+)\\]`));
  if (!match) throw new Error(`no ${role} "${name}" in snapshot:\n${snapshot}`);
  return match[1];
}

type ActPage = Page & { completed: number; failure: { index: number; code: string; message: string } | null };

test("acts on the page through refs: select, wait, type and submit", async () => {
  const page = pageOf(await call("navigate", { taskId: 20, target: { url: `${origin}/act` }, context: context(roomy) }));
  const select = refOf(page.snapshot, "combobox", "Color");
  const load = refOf(page.snapshot, "button", "Load");
  const query = refOf(page.snapshot, "textbox", "Query");

  const first = pageOf(await call("act", { taskId: 20, actions: [
    { type: "select", ref: select, values: ["Blue"] },
    clickOn(load),
    { type: "wait_for", text: "loaded later", seconds: 5 },
  ] })) as ActPage;
  expect(first).toMatchObject({ completed: 3, failure: null, title: "color:b" });
  expect(first.snapshot).toContain("loaded later");

  const submitted = pageOf(await call("act", { taskId: 20, actions: [{ type: "type", ref: query, text: "cosir", clear: true, submit: true, keyByKey: false }] })) as ActPage;
  expect(submitted).toMatchObject({ completed: 1, failure: null, title: "Done" });
  expect(submitted.url).toBe(`${origin}/done?q=cosir`);

  // 目的：导航后旧 ref 一律失效；潜在缺陷：旧序号恰好指到新页面的另一个元素，点错东西。
  const stale = pageOf(await call("act", { taskId: 20, actions: [clickOn(load)] })) as ActPage;
  expect(stale).toMatchObject({ completed: 0, failure: { index: 0, code: "REF_STALE" }, title: "Done" });
});

// 目的：长页面后半段的元素也能读到并操作，ref 跨快照不变；潜在缺陷：截断之后的内容对 Agent 永远不可见。
test("pages long snapshots with stable refs, then hovers, presses shortcuts and scrolls areas", async () => {
  const page = pageOf(await call("navigate", { taskId: 24, target: { url: `${origin}/rich` }, context: context(roomy) }));
  const note = page.snapshot.split("\n").at(-1)!;
  expect(note).toMatch(/read on with from_line=\d+$/);
  const menu = refOf(page.snapshot, "button", "Menu");
  const inside = refOf(page.snapshot, "button", "Inside");

  // 按截断提示一页页往下读，直到最后一页。
  let rest = page.snapshot;
  for (let next = rest.match(/read on with from_line=(\d+)$/); next; next = rest.match(/read on with from_line=(\d+)$/)) {
    rest = pageOf(await call("snapshot", { taskId: 24, view: { fromLine: Number(next[1]) } })).snapshot;
  }
  const last = refOf(rest, "link", "Item 799");
  expect(refOf(pageOf(await call("snapshot", { taskId: 24, view: { fromLine: 1 } })).snapshot, "button", "Menu")).toBe(menu);

  const acted = pageOf(await call("act", { taskId: 24, actions: [
    { type: "wait_for", textGone: "Loading", seconds: 5 },
    { type: "hover", target: { ref: menu } },
    { type: "press", key: "k", modifiers: ["ControlOrMeta"] },
    { type: "scroll", direction: "down", ref: inside },
    { type: "scroll", direction: "down" },
    { type: "scroll", ref: last },
  ] })) as ActPage;
  expect(acted).toMatchObject({ completed: 6, failure: null, title: "shortcut" });
  expect(acted.snapshot).toContain("Hidden item");

  // 目的：滚到底要报出来；潜在缺陷：无限滚动的批次空转，模型以为加载了更多。
  const end = pageOf(await call("act", { taskId: 24, actions: [{ type: "scroll", direction: "down", ref: inside }] })) as ActPage;
  expect(end).toMatchObject({ completed: 0, failure: { code: "SCROLL_END", message: "this area is already at the bottom" } });
});

// 目的：Agent 能把本机文件交给网页——经它的点击打开的选文件框，或直接交给文件输入框；潜在缺陷：选文件框被拦下后
// 无路可走，或文件没进输入框、页面读到空文件。也核对 target=_blank 链接开成同一 Task 的标签。
test("uploads through the file chooser an action opened, and opens target=_blank links as tabs of the same task", async () => {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "cosir-upload-")), "report.txt");
  fs.writeFileSync(file, "hello");
  const page = pageOf(await call("navigate", { taskId: 21, target: { url: `${origin}/act` }, context: context(roomy) }));
  const upload = refOf(page.snapshot, "button", "Upload");
  const popup = refOf(page.snapshot, "link", "Popup");
  const after = pageOf(await call("act", { taskId: 21, actions: [clickOn(upload), { type: "upload", paths: [file] }, clickOn(popup)] })) as ActPage;
  expect(after).toMatchObject({ completed: 3, failure: null });
  const chosen = "(() => { const [f] = document.querySelector('input[type=file]').files; return f.name + ':' + f.size; })()";
  expect(valueOf(await link("browser.evaluate", 21, { expression: chosen })).value).toBe('"report.txt:5"');
  await expect.poll(async () => {
    const tabs = await call("manageTabs", { taskId: 21, action: "list" });
    return tabs.ok ? (tabs.value as unknown as unknown[]).length : 0;
  }).toBe(2);
  const notes = [...after.notes, ...pageOf(await call("snapshot", { taskId: 21 })).notes].join("\n");
  expect(notes).toContain("the page opened a file chooser");
  expect(notes).toMatch(/opened .*\/second in a new tab t\d+/);

  // 直接交给文件输入框（经线上形状）；宿主只收后端解析好的绝对路径。
  const direct = valueOf(await link("browser.act", 21, { tab: after.tabId, actions: [{ type: "upload", paths: [file], ref: upload }] }));
  expect(direct).toMatchObject({ completed: 1, failure: null });
  expect(await link("browser.act", 21, { tab: after.tabId, actions: [{ type: "upload", paths: ["report.txt"], ref: upload }] })).toMatchObject({ ok: false, code: "INVALID_PARAMS" });
});

// 目的：用户正在页面上操作时 Agent 不抢；潜在缺陷：Agent 的点击打断用户的登录输入。
test("refuses to act while the user is typing in the page", async () => {
  // 唯一的地址：按地址找网页视图，不能与别的用例的标签重名。
  const page = pageOf(await call("navigate", { taskId: 22, target: { url: `${origin}/act?typing` }, context: context(roomy) }));
  await app.evaluate(({ webContents }, tabUrl) => {
    const contents = webContents.getAllWebContents().find((item) => item.getURL() === tabUrl)!;
    contents.sendInputEvent({ type: "keyDown", keyCode: "A" });
  }, page.url);
  // sendInputEvent 异步派发：等事件到达页面，再让 Agent 动手。
  await new Promise((resolve) => setTimeout(resolve, 200));
  const refused = await call("act", { taskId: 22, actions: [{ type: "press", key: "Tab" }] });
  expect(refused).toMatchObject({ ok: false, code: "USER_ACTIVE" });
});

test("screenshots in CSS pixels and manages the task's tabs", async () => {
  await call("navigate", { taskId: 23, target: { url: `${origin}/form` }, context: context(roomy) });
  const second = pageOf(await call("navigate", { taskId: 23, newTab: true, target: { url: `${origin}/second` }, context: context(roomy) }));

  const shot = await call("screenshot", { taskId: 23, fullPage: false });
  expect(shot.ok).toBe(true);
  const size = await app.evaluate(({ nativeImage }, image) => nativeImage.createFromDataURL(image).getSize(), (shot as unknown as { value: { image: string } }).value.image);
  expect(size).toEqual({ width: 1280, height: 800 });

  const listed = await call("manageTabs", { taskId: 23, action: "list" });
  expect(listed).toMatchObject({ ok: true, value: [{ current: false }, { tabId: second.tabId, current: true, title: "Second" }] });
  const first = (listed as unknown as { value: { tabId: string }[] }).value[0].tabId;
  expect(await call("manageTabs", { taskId: 23, action: "select", tab: first })).toMatchObject({ ok: true, value: [{ tabId: first, current: true }, { current: false }] });
  expect(await call("manageTabs", { taskId: 23, action: "close", tab: first })).toMatchObject({ ok: true, value: [{ tabId: second.tabId, current: true }] });
});

// 目的：初始化前就归 Host 持有；删除 Task 同时回收真实 Chromium 资源，迟到初始化不能复活标签。
test("drops a tab during initialization and destroys its webContents", async () => {
  const result = await app.evaluate(async () => {
    type HarnessHost = {
      userOpen(taskId: number, context: { limits: { perTask: number; total: number }; downloadDir: string }): Promise<string>;
      manageTabs(request: { taskId: number; action: "list"; context: unknown }): { tabId: string }[];
      dropTasks(ids: number[]): void;
      tabs: Map<string, { tab: { view: Electron.WebContentsView } }>;
    };
    const { host } = (globalThis as unknown as { browserHarness: { host: HarnessHost } }).browserHarness;
    const context = { limits: { perTask: 5, total: 50 }, downloadDir: "/tmp" };
    const opening = host.userOpen(99, context);
    const listed = host.manageTabs({ taskId: 99, action: "list", context });
    const contents = host.tabs.get(listed[0].tabId)!.tab.view.webContents;
    const destroyed = new Promise<void>((resolve) => contents.once("destroyed", () => resolve()));
    host.dropTasks([99]);
    const code = await opening.then(() => "opened", (error: { code: string }) => error.code);
    await destroyed;
    return { listed: listed.length, code, destroyed: contents.isDestroyed(), remaining: host.manageTabs({ taskId: 99, action: "list", context }) };
  });
  expect(result).toEqual({ listed: 1, code: "TAB_NOT_FOUND", destroyed: true, remaining: [] });
});


// 目的：弹窗式登录要把结果交回来源页；潜在缺陷：重开新标签丢了 opener，登录成功但来源页永远收不到。
test("keeps the opener for windows the page opens, and drops them when they close themselves", async () => {
  const page = pageOf(await call("navigate", { taskId: 30, target: { url: `${origin}/opener` }, context: context(roomy) }));
  const signIn = refOf(page.snapshot, "button", "Sign in");
  // 备注读一次就清空：每次快照的都收起来。
  const notes = [...pageOf(await call("act", { taskId: 30, actions: [clickOn(signIn)] })).notes];
  const look = async () => {
    const opener = pageOf(await call("snapshot", { taskId: 30, tab: page.tabId }));
    notes.push(...opener.notes);
    return opener.title;
  };
  await expect.poll(look).toBe("got:token-123");
  await expect.poll(async () => {
    const tabs = await call("manageTabs", { taskId: 30, action: "list" });
    return tabs.ok ? (tabs.value as unknown as unknown[]).length : -1;
  }).toBe(1);
  await look();
  expect(notes.join("\n")).toMatch(/opened .*\/child in a new tab t\d+/);
  expect(notes.join("\n")).toMatch(/tab t\d+ \(opened by this page\) closed itself/);
});

// 目的：下载落到 Task 的下载目录并告诉 Agent 路径；潜在缺陷：下载被静默丢掉，Agent 以为导出没反应。
test("saves downloads into the tab's download directory and reports them", async () => {
  const page = pageOf(await call("navigate", { taskId: 31, target: { url: `${origin}/download` }, context: context(roomy) }));
  const clicked = pageOf(await call("act", { taskId: 31, actions: [clickOn(refOf(page.snapshot, "link", "Export"))] }));
  const target = path.join(DOWNLOAD_DIR, "report.csv");
  await expect.poll(() => fs.existsSync(target) && fs.readFileSync(target, "utf8")).toBe("a,b\n1,2\n");
  const downloads = await app.evaluate((_electron, tabId) => {
    const { host } = (globalThis as unknown as { browserHarness: { host: { state(): { downloads: { tabId: string }[] } } } }).browserHarness;
    return host.state().downloads.filter((item) => item.tabId === tabId);
  }, page.tabId);
  expect(downloads).toMatchObject([{ filename: "report.csv", path: target, state: "completed" }]);
  // 小文件可能在动作返回前就下完：「完成」备注随动作结果或之后的快照到来。
  const notes = [...clicked.notes, ...pageOf(await call("snapshot", { taskId: 31 })).notes].join("\n");
  expect(notes).toContain(`finished: ${target}`);
});

// 目的：面板缩放显示时 Agent 的视口与点击不变；潜在缺陷：视口随面板变窄触发移动版布局，或合成点击按视图坐标落到别处。
test("keeps the agent's viewport fixed and clicks accurately while the tab is shown scaled", async () => {
  const page = pageOf(await call("navigate", { taskId: 32, target: { url: `${origin}/far` }, context: context(roomy) }));
  const width = await app.evaluate(async (_electron, tabId) => {
    type Harness = { host: { place(tabId: string, rect: unknown): void; tabs: Map<string, { tab: { view: Electron.WebContentsView } }> } };
    const { host } = (globalThis as unknown as { browserHarness: Harness }).browserHarness;
    host.place(tabId, { x: 0, y: 0, width: 640, height: 400 });
    await new Promise((resolve) => setTimeout(resolve, 300));
    return host.tabs.get(tabId)!.tab.view.webContents.executeJavaScript("innerWidth");
  }, page.tabId);
  expect(width).toBe(1280);
  const clicked = pageOf(await call("act", { taskId: 32, actions: [clickOn(refOf(page.snapshot, "button", "Far"))] }));
  expect(clicked.title).toBe("far clicked");
  const shot = await call("screenshot", { taskId: 32, fullPage: false });
  const size = await app.evaluate(({ nativeImage }, image) => nativeImage.createFromDataURL(image).getSize(), (shot as unknown as { value: { image: string } }).value.image);
  expect(size).toEqual({ width: 1280, height: 800 });
  await app.evaluate((_electron, tabId) => {
    (globalThis as unknown as { browserHarness: { host: { place(tabId: string, rect: unknown): void } } }).browserHarness.host.place(tabId, null);
  }, page.tabId);
});

/** 当前 Task 的标签 id 列表。 */
async function tabIds(taskId: number): Promise<string[]> {
  const tabs = await call("manageTabs", { taskId, action: "list" });
  return tabs.ok ? (tabs.value as unknown as { tabId: string }[]).map((item) => item.tabId) : [];
}

// 目的：页面开出的窗口一加载就弹窗时等人作答；潜在缺陷：初始化撞上弹窗失败，标签被直接关掉。
test("keeps a window that alerts while loading until its dialog is answered", async () => {
  const page = pageOf(await call("navigate", { taskId: 40, target: { url: `${origin}/popup-alert` } }));
  await call("act", { taskId: 40, actions: [clickOn(refOf(page.snapshot, "button", "Open"))] });
  await expect.poll(() => tabIds(40)).toHaveLength(2);
  const popup = (await tabIds(40))[1];
  const paused = pageOf(await call("snapshot", { taskId: 40, tab: popup }));
  expect(paused.dialog).toMatchObject({ type: "alert", message: "Sign-in failed" });
  const answered = pageOf(await call("act", { taskId: 40, tab: popup, actions: [{ type: "dialog", accept: true, id: paused.dialog!.id }] })) as ActPage;
  expect(answered).toMatchObject({ completed: 1, failure: null, dialog: null, title: "after alert" });
});

// 目的：过时的作答不落到新弹窗上；潜在缺陷：用户点的是上一个弹窗的「确定」，却确认了紧接着弹出的那个。
test("ignores an answer meant for a dialog that is no longer showing", async () => {
  await call("navigate", { taskId: 41, target: { url: `${origin}/two-dialogs` } });
  await expect.poll(async () => pageOf(await call("snapshot", { taskId: 41 })).dialog?.type).toBe("alert");
  const first = pageOf(await call("snapshot", { taskId: 41 })).dialog!;
  await call("act", { taskId: 41, actions: [{ type: "dialog", accept: true, id: first.id }] });
  await expect.poll(async () => pageOf(await call("snapshot", { taskId: 41 })).dialog?.type).toBe("confirm");
  await app.evaluate((_electron, { id }) => {
    type Harness = { host: { state(): { tabs: { tabId: string; taskId: number }[] }; userAnswerDialog(tab: string, id: number, answer: unknown): void } };
    const { host } = (globalThis as unknown as { browserHarness: Harness }).browserHarness;
    const tab = host.state().tabs.find((item) => item.taskId === 41)!;
    host.userAnswerDialog(tab.tabId, id, { accept: true });
  }, { id: first.id });
  expect(pageOf(await call("snapshot", { taskId: 41 })).dialog).toMatchObject({ type: "confirm", message: "second?" });
});

// 目的：同进程的标签一起停住时指明去哪答；潜在缺陷：来源标签的 CDP 空等 10 秒报「无响应」。
test("tells a tab that shares a paused process which tab to answer", async () => {
  const page = pageOf(await call("navigate", { taskId: 42, target: { url: `${origin}/popup-confirm` } }));
  await call("act", { taskId: 42, actions: [clickOn(refOf(page.snapshot, "button", "Open"))] });
  await expect.poll(() => tabIds(42)).toHaveLength(2);
  const popup = (await tabIds(42))[1];
  await expect.poll(async () => pageOf(await call("snapshot", { taskId: 42, tab: popup })).dialog?.type).toBe("confirm");
  const started = Date.now();
  const opener = await call("snapshot", { taskId: 42, tab: page.tabId });
  expect(opener).toMatchObject({ ok: false, code: "DIALOG_OPEN" });
  expect(opener.ok ? "" : opener.message).toContain(`tab ${popup} (this task)`);
  expect(Date.now() - started).toBeLessThan(5_000);
  const dialog = pageOf(await call("snapshot", { taskId: 42, tab: popup })).dialog!;
  await call("act", { taskId: 42, tab: popup, actions: [{ type: "dialog", accept: true, id: dialog.id }] });
  expect(await call("snapshot", { taskId: 42, tab: page.tabId })).toMatchObject({ ok: true });
});

// 目的：下载归 Task 不归标签；潜在缺陷：弹出页下载后自己关闭，下载被一起取消、结果也没人收到。
test("finishes a download started by a window that then closes itself", async () => {
  const page = pageOf(await call("navigate", { taskId: 43, target: { url: `${origin}/popup-download` } }));
  const notes = [...pageOf(await call("act", { taskId: 43, actions: [clickOn(refOf(page.snapshot, "button", "Export"))] })).notes];
  await expect.poll(async () => {
    notes.push(...pageOf(await call("snapshot", { taskId: 43 })).notes);
    return (await tabIds(43)).length;
  }).toBe(1);
  await expect.poll(async () => {
    notes.push(...pageOf(await call("snapshot", { taskId: 43 })).notes);
    return notes.join("\n");
  }).toMatch(/the download "report.*\.csv" finished/);
  const states = await app.evaluate(() => {
    const { host } = (globalThis as unknown as { browserHarness: { host: { state(): { downloads: { taskId: number; state: string }[] } } } }).browserHarness;
    return host.state().downloads.filter((item) => item.taskId === 43).map((item) => item.state);
  });
  expect(states).toEqual(["completed"]);
});


// 目的：主文档的 HTTP 状态码要报出来；潜在缺陷：404 / 500 页面按成功返回，Agent 以为页面正常。也经线上路径核对字段。
test("reports the main document's HTTP status over the wire", async () => {
  const missing = valueOf(await link("browser.navigate", 50, { url: `${origin}/does-not-exist` }));
  expect(missing).toMatchObject({ status: 404, emulation: { width: 1280, height: 800, mobile: false, color_scheme: null } });
  // 主文档请求在导航提交前就记下，仍算这个页面的：默认读取（不给 since）就看得到它。
  const documents = valueOf(await link("browser.logs", 50, { kind: "network", failed_only: true }));
  expect(documents.entries).toMatchObject([{ url: `${origin}/does-not-exist`, status: 404, resource_type: "Document" }]);
  const ok = valueOf(await link("browser.navigate", 50, { url: `${origin}/form` }));
  expect(ok).toMatchObject({ status: 200, title: "Form", new_logs: { console_errors: 0, console_warnings: 0, failed_requests: 0 } });
  // 主框架没加载成功：页面已不是上一个文档，不能沿用它的状态码。
  expect(await link("browser.navigate", 50, { url: "http://127.0.0.1:9/" })).toMatchObject({ ok: false, code: "NAVIGATION_FAILED" });
  expect(valueOf(await link("browser.snapshot", 50, {})).status).toBeNull();
  expect(await link("browser.act", 50, { actions: [{ type: "click", ref: "e1", bogus: true }] })).toMatchObject({ ok: false, code: "INVALID_PARAMS" });
});

// 目的：导航没有打开新页面（转成下载、204）时及时返回原页面并说明；潜在缺陷：加载事件一个都不来，空等满 30 秒
// 报失败，模型重试又下载一次。
test("returns promptly with a note when a navigation opens no new page", async () => {
  valueOf(await link("browser.navigate", 54, { url: `${origin}/form` }));
  for (const target of ["/report.csv", "/no-content"]) {
    const started = Date.now();
    const page = valueOf(await link("browser.navigate", 54, { url: `${origin}${target}` }));
    expect(Date.now() - started, target).toBeLessThan(5_000);
    expect(page, target).toMatchObject({ title: "Form", url: `${origin}/form`, status: 200 });
    expect((page.notes as string[]).join("\n"), target).toContain(`opening ${origin}${target} did not open a new page`);
  }
  // 页面还是原来的：日志的「当前页面」没有被那次没换文档的导航挪走。
  const documents = valueOf(await link("browser.logs", 54, { kind: "network", pattern: "/form" }));
  expect(documents.entries).toMatchObject([{ url: `${origin}/form`, status: 200, resource_type: "Document" }]);
});

// 目的：唯一的结束信号是加载状态结束；潜在缺陷：重定向或加载中脚本跳转时加载状态中途结束，提前返回旧页面或误报「没有打开」。
test("follows redirects, script redirects and history to the page that ends up loaded", async () => {
  valueOf(await link("browser.navigate", 55, { url: `${origin}/form` }));
  const redirected = valueOf(await link("browser.navigate", 55, { url: `${origin}/moved` }));
  expect(redirected).toMatchObject({ title: "Responsive", url: `${origin}/responsive`, status: 200 });
  const bounced = valueOf(await link("browser.navigate", 55, { url: `${origin}/bounce` }));
  expect(bounced).toMatchObject({ title: "Responsive", url: `${origin}/responsive` });
  for (const page of [redirected, bounced]) expect((page.notes as string[]).join("\n")).not.toContain("did not open");
  const back = valueOf(await link("browser.navigate", 55, { action: "back" }));
  const forward = valueOf(await link("browser.navigate", 55, { action: "forward" }));
  for (const page of [back, forward]) {
    expect(page).toMatchObject({ title: "Responsive", status: 200 });
    expect((page.notes as string[]).join("\n")).not.toContain("did not open");
  }
  const hash = valueOf(await link("browser.navigate", 55, { url: `${origin}/responsive#later` }));
  expect(hash).toMatchObject({ url: `${origin}/responsive#later` });
});

// 目的：编码时排查页面问题要看得到 console 与网络；潜在缺陷：异常与失败请求丢失，或读日志吞掉了页面结果的计数。
test("captures console messages, uncaught errors and failed requests, and pages through them", async () => {
  const page = valueOf(await link("browser.navigate", 51, { url: `${origin}/noisy` }));
  await new Promise((resolve) => setTimeout(resolve, 300));
  const errors = valueOf(await link("browser.logs", 51, { kind: "console", level: "error" }));
  const texts = (errors.entries as { text: string }[]).map((entry) => entry.text).join("\n");
  expect(texts).toContain("bad thing");
  expect(texts).toContain("uncaught boom");
  expect(texts).not.toContain("hello log");
  expect(texts).not.toContain("Electron Security Warning");
  const failed = valueOf(await link("browser.logs", 51, { kind: "network", failed_only: true }));
  expect(failed.entries).toMatchObject([{ method: "GET", url: `${origin}/api/missing`, status: 404, resource_type: "Fetch" }]);
  // 读日志不消费页面结果的「上次以来」：导航结果与随后快照的计数合起来恰好是全部新错误（不重复、不遗漏）。
  const snapshot = valueOf(await link("browser.snapshot", 51, {}));
  const counted = (result: Record<string, unknown>) => result.new_logs as { console_errors: number; failed_requests: number };
  expect(counted(snapshot).console_errors + counted(page).console_errors).toBe(2);
  expect(counted(snapshot).failed_requests + counted(page).failed_requests).toBe(1);
  expect(valueOf(await link("browser.snapshot", 51, {})).new_logs).toEqual({ console_errors: 0, console_warnings: 0, failed_requests: 0 });
  const all = valueOf(await link("browser.logs", 51, { kind: "console", since: 0 }));
  const fresh = valueOf(await link("browser.logs", 51, { kind: "console", since: all.next as number }));
  expect(fresh.entries).toEqual([]);
});

// 目的：执行 JS 拿到值或错误，脚本弹窗时页面停住要报出来；潜在缺陷：抛错被当作调用失败，或弹窗让调用挂到超时。
test("evaluates expressions, reports script errors and dialogs the script opens", async () => {
  await link("browser.navigate", 52, { url: `${origin}/form` });
  // 经 Electron 的 debugger 传回的对象键按字母序（Chromium 内部的有序字典），按 JSON 语义比较。
  const object = valueOf(await link("browser.evaluate", 52, { expression: "({ title: document.title, n: [1, 2].length })" }));
  expect(object).toMatchObject({ truncated: false, error: null, dialog: null });
  expect(JSON.parse(object.value as string)).toEqual({ title: "Form", n: 2 });
  expect(valueOf(await link("browser.evaluate", 52, { expression: "new Promise((resolve) => setTimeout(() => resolve('later'), 50))" }))).toMatchObject({ value: "\"later\"" });
  expect(valueOf(await link("browser.evaluate", 52, { expression: "undefined" }))).toMatchObject({ value: "undefined" });
  const failed = valueOf(await link("browser.evaluate", 52, { expression: "missingThing.prop" }));
  expect(failed).toMatchObject({ value: null, dialog: null });
  expect(String(failed.error)).toContain("ReferenceError");
  // 抛出原始值（没有 Error 的描述）时报出值本身。
  expect(String(valueOf(await link("browser.evaluate", 52, { expression: "Promise.reject('nope')" })).error)).toContain('"nope"');
  const paused = valueOf(await link("browser.evaluate", 52, { expression: "document.title = 'answered:' + confirm('Proceed?')" }));
  expect(paused).toMatchObject({ value: null, error: null, dialog: { type: "confirm", message: "Proceed?" } });
  // 弹窗开着时再执行脚本：脚本根本没跑，整次失败，不能把这个弹窗算到新脚本头上。
  expect(await link("browser.evaluate", 52, { expression: "1" })).toMatchObject({ ok: false, code: "DIALOG_OPEN" });
  const answered = valueOf(await link("browser.act", 52, { actions: [{ type: "dialog", accept: true, id: (paused.dialog as { id: number }).id }] }));
  expect(answered).toMatchObject({ completed: 1, failure: null, dialog: null });
  await expect.poll(async () => valueOf(await link("browser.evaluate", 52, { expression: "document.title" })).value).toBe("\"answered:true\"");
  // 只在等一个不落定的 Promise 时主线程空闲：超时后不能发打断（会误杀页面接下来执行的脚本），页面照常运转。
  await link("browser.evaluate", 52, { expression: "window.ticks = 0; setInterval(() => window.ticks++, 20); 0" });
  const waited = valueOf(await link("browser.evaluate", 52, { expression: "new Promise(() => {})" }));
  expect(String(waited.error)).toContain("waiting on a Promise");
  const before = Number(valueOf(await link("browser.evaluate", 52, { expression: "window.ticks" })).value);
  await expect.poll(async () => Number(valueOf(await link("browser.evaluate", 52, { expression: "window.ticks" })).value)).toBeGreaterThan(before);
  // 转 JSON 卡住（getter 永不返回）同样被打断，标签还能接着用。
  const getter = valueOf(await link("browser.evaluate", 52, { expression: "({ get x() { while (true) {} } })" }));
  expect(String(getter.error)).toContain("was stopped");
  expect(valueOf(await link("browser.evaluate", 52, { expression: "2 + 2" })).value).toBe("4");
  // 死循环超时后被打断：标签还能接着用，不会一直卡在占满的主线程上。
  const looped = valueOf(await link("browser.evaluate", 52, { expression: "while (true) {}" }));
  expect(String(looped.error)).toContain("was stopped");
  expect(valueOf(await link("browser.evaluate", 52, { expression: "1 + 1" })).value).toBe("2");
  // 标签在执行途中关掉：以 TAB_NOT_FOUND 结束，不挂到时限、也不报成宿主内部错误。
  const hanging = link("browser.evaluate", 52, { expression: "new Promise(() => {})" });
  valueOf(await link("browser.tabs", 52, { action: "close" }));
  expect(await hanging).toMatchObject({ ok: false, code: "TAB_NOT_FOUND" });
});

// 目的：测试响应式与深浅色；潜在缺陷：模拟只改一半（视口变了深浅色没变）、导航后丢失，或面板摆放没跟上新长宽比。
test("emulates a viewport and colour scheme before loading, and keeps them across navigations", async () => {
  // 手机布局下，没声明 viewport meta 的页面按 980px 的桌面宽度排版（与真实手机一致），所以用声明了的页面。
  // 先挂到面板上（占位 800×800）：改视口后视图要按新长宽比重新摆放。
  const opened = valueOf(await link("browser.navigate", 53, { url: `${origin}/responsive` }));
  const placed = () => app.evaluate((_electron, tabId) => {
    type Harness = { host: { tabs: Map<string, { tab: { view: Electron.WebContentsView } }> } };
    return (globalThis as unknown as { browserHarness: Harness }).browserHarness.host.tabs.get(tabId)!.tab.view.getBounds();
  }, opened.tab_id as string);
  await app.evaluate((_electron, tabId) => {
    (globalThis as unknown as { browserHarness: { host: { place(tabId: string, rect: unknown): void } } }).browserHarness.host.place(tabId, { x: 0, y: 0, width: 800, height: 800 });
  }, opened.tab_id as string);
  expect(await placed()).toEqual({ x: 0, y: 0, width: 800, height: 500 });
  const page = valueOf(await link("browser.navigate", 53, { url: `${origin}/responsive`, viewport: { width: 390, height: 844, mobile: true }, color_scheme: "dark" }));
  expect(page.emulation).toEqual({ width: 390, height: 844, mobile: true, color_scheme: "dark" });
  // 390×844 放进 800×800：高度顶满，宽 370，水平居中。
  await expect.poll(placed).toEqual({ x: 215, y: 0, width: 370, height: 800 });
  const probe = "[innerWidth, matchMedia('(prefers-color-scheme: dark)').matches, navigator.maxTouchPoints > 0]";
  expect(valueOf(await link("browser.evaluate", 53, { expression: probe })).value).toBe("[390,true,true]");
  await link("browser.navigate", 53, { url: `${origin}/responsive?again` });
  expect(valueOf(await link("browser.evaluate", 53, { expression: probe })).value).toBe("[390,true,true]");
  const shot = await call("screenshot", { taskId: 53, fullPage: false });
  const size = await app.evaluate(({ nativeImage }, image) => nativeImage.createFromDataURL(image).getSize(), (shot as unknown as { value: { image: string } }).value.image);
  expect(size).toEqual({ width: 390, height: 844 });
  await app.evaluate((_electron, tabId) => {
    (globalThis as unknown as { browserHarness: { host: { place(tabId: string, rect: unknown): void } } }).browserHarness.host.place(tabId, null);
  }, opened.tab_id as string);
  const reset = valueOf(await link("browser.navigate", 53, { action: "reload", viewport: { width: 1280, height: 800 }, color_scheme: "auto" }));
  expect(reset.emulation).toEqual({ width: 1280, height: 800, mobile: false, color_scheme: null });
  // 不在面板上时，视图随生效的视口改尺寸（合成输入要落在视图之内）。
  expect(await placed()).toEqual({ x: 0, y: 0, width: 1280, height: 800 });
  expect(valueOf(await link("browser.evaluate", 53, { expression: "[innerWidth, navigator.maxTouchPoints > 0]" })).value).toBe("[1280,false]");
});

// 目的：「看起来可点」的无语义元素也能被 Agent 指定，链接带地址；潜在缺陷：`<div onclick>` 被当成容器折叠掉，模型读得到
// 文字却点不到；cursor 继承让卡片里每一层都拿到 ref。
test("gives refs to elements that only look clickable, once per card, and shows link addresses", async () => {
  const page = pageOf(await call("navigate", { taskId: 60, target: { url: `${origin}/clickable` }, context: context(roomy) }));
  const card = page.snapshot.match(/generic \[ref=(e\d+)\] \[cursor=pointer\]\n\s+- text "Open card/)?.[1];
  expect(card, page.snapshot).toBeDefined();
  expect(page.snapshot.match(/\[cursor=pointer\]/g)).toHaveLength(1);
  expect(page.snapshot).toMatch(/link "Next" \[ref=e\d+\] url="\/second\?q=1"/);
  expect(page.snapshot).toContain('url="https://example.com/x"');
  const clicked = pageOf(await call("act", { taskId: 60, actions: [clickOn(card)] }));
  expect(clicked.title).toBe("card");
});

// 目的：快照里没有 ref 的地方（画布、拖放区）也能用坐标操作，右键、双击、拖放与真实输入一致；潜在缺陷：右键不触发
// contextmenu、双击只触发 click、合成的拖动不触发 HTML5 drop。
test("clicks, double clicks and drags at viewport coordinates", async () => {
  valueOf(await link("browser.navigate", 61, { url: `${origin}/pointer` }));
  const act = async (action: Record<string, unknown>) => valueOf(await link("browser.act", 61, { actions: [action] }));
  expect(await act({ type: "click", x: 120, y: 60, button: "right" })).toMatchObject({ failure: null, title: "menu 120,60" });
  expect(await act({ type: "click", x: 10, y: 10, click_count: 2, modifiers: ["Shift"] })).toMatchObject({ failure: null, title: "double true" });
  expect(await act({ type: "drag", start: { x: 50, y: 320 }, end: { x: 350, y: 320 } })).toMatchObject({ failure: null, title: "dropped a" });
  expect(await act({ type: "hover", x: 5000, y: 10 })).toMatchObject({ completed: 0, failure: { code: "OUT_OF_VIEWPORT" } });

  // 挂在面板上、缩放显示时也要落在终点（真实应用验收发现：拖放事件按视图坐标发会落到一半处）。
  valueOf(await link("browser.navigate", 61, { action: "reload" }));
  await app.evaluate((_electron, tab) => {
    const { host } = (globalThis as unknown as { browserHarness: { host: { place(tab: string, box: object): void } } }).browserHarness;
    host.place(tab, { x: 0, y: 0, width: 640, height: 400 });
  }, (await act({ type: "hover", x: 1, y: 1 })).tab_id as string);
  expect(await act({ type: "drag", start: { x: 50, y: 320 }, end: { x: 350, y: 320 } })).toMatchObject({ failure: null, title: "dropped a" });
});

// 目的：按键盘事件工作的控件（自动补全、验证码格子）收得到每个按键；潜在缺陷：一次写入只触发 input，keyup 一个都没有；
// 中文等键盘上没有的字符逐键时丢掉或变成别的字。
test("types key by key so the page sees a key event per character", async () => {
  const page = valueOf(await link("browser.navigate", 62, { url: `${origin}/keys` }));
  const field = refOf(page.snapshot as string, "textbox", "Code");
  const typed = valueOf(await link("browser.act", 62, { actions: [{ type: "type", ref: field, text: "a1中", key_by_key: true }] }));
  expect(typed).toMatchObject({ failure: null, title: "3 a1中" });
});

// 目的：iframe 里的元素也能看到、能操作（同源与跨站的跨进程 iframe）；潜在缺陷：只读主框架的树，嵌入的表单看不见；
// 跨进程框架的节点 id 与主框架重号而点错元素，或框架内坐标当主视口坐标点空（iframe 在首屏之下，要先滚入视野）。
test("reads and acts inside same-origin and cross-site iframes", async () => {
  const page = valueOf(await link("browser.navigate", 63, { url: `${origin}/frames` }));
  const snapshot = page.snapshot as string;
  expect(snapshot).toMatch(/- iframe "Same"\n\s+- button "In same" \[ref=e\d+\]/);
  expect(snapshot).toMatch(/- iframe "Cross"\n\s+- textbox "Note" \[ref=e\d+\]/);
  expect(page.frames).toBe(0);
  const act = async (actions: Record<string, unknown>[]) => valueOf(await link("browser.act", 63, { actions }));
  expect(await act([{ type: "click", ref: refOf(snapshot, "button", "In same") }])).toMatchObject({ failure: null, title: "same clicked" });
  const sent = await act([
    { type: "type", ref: refOf(snapshot, "textbox", "Note"), text: "hi from frame" },
    { type: "click", ref: refOf(snapshot, "button", "Send") },
  ]);
  expect(sent).toMatchObject({ failure: null, title: "got hi from frame" });
  // 跨进程框架里的选文件框同样交给 upload，不弹系统选择框。
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "cosir-upload-")), "frame.txt");
  fs.writeFileSync(file, "x");
  const attached = await act([{ type: "click", ref: refOf(snapshot, "button", "Attach") }, { type: "upload", paths: [file] }]);
  expect(attached).toMatchObject({ failure: null, title: "got file frame.txt" });
});

// 目的：长页面上只读屏幕上的部分或按关键词找；潜在缺陷：几何按 CSS 像素算（Retina 上差一倍）、忘了减滚动，列出的是
// 另一块区域；固定定位的页头滚动后丢掉；iframe 里的内容滚到眼前也不算在视口里。
test("reads only the viewport or the lines matching a query", async () => {
  valueOf(await link("browser.navigate", 64, { url: `${origin}/viewport` }));
  const viewport = async () => valueOf(await link("browser.snapshot", 64, { scope: "viewport" })).snapshot as string;
  const top = await viewport();
  expect(top).toContain('button "Fixed menu"');
  expect(top).toContain('button "Row 1"');
  expect(top).not.toContain('button "Row 20"');
  expect(top).not.toContain("In same");

  valueOf(await link("browser.evaluate", 64, { expression: "scrollTo(0, document.body.scrollHeight)" }));
  const bottom = await viewport();
  expect(bottom).toContain('button "Fixed menu"');
  expect(bottom).toContain('button "Row 60"');
  expect(bottom).toContain('button "In same"');
  expect(bottom).not.toContain('button "Row 1"');

  const found = valueOf(await link("browser.snapshot", 64, { find: "row 3" })).snapshot as string;
  expect(found).toMatch(/^\(showing lines matching "row 3"/);
  expect(found).toContain('button "Row 3"');
  expect(found).toContain('button "Row 39"');
  expect(found).not.toContain("Fixed menu");
});

// 目的：act 之后只回传变化，省上下文；潜在缺陷：每步都回传整页（长页面几步就占满上下文），或换了页面还给增量、
// 模型拿不到新页面的 ref。
test("returns only what changed after an action, and the whole page after it navigates", async () => {
  const page = valueOf(await link("browser.navigate", 65, { url: `${origin}/counter` }));
  const add = refOf(page.snapshot as string, "button", "Add");
  const changed = valueOf(await link("browser.act", 65, { actions: [{ type: "click", ref: add }] })).snapshot as string;
  expect(changed).toMatch(/^\(changes since this tab's last browser result: 1 new or changed, 0 removed;/);
  expect(changed).toContain('text "1" [changed]');
  expect(changed).not.toContain("Item 1");
  const still = valueOf(await link("browser.act", 65, { actions: [{ type: "hover", ref: add }] })).snapshot as string;
  expect(still).toBe("(nothing on the page changed since this tab's last browser result)");
  const moved = valueOf(await link("browser.act", 65, { actions: [{ type: "click", ref: refOf(page.snapshot as string, "link", "Elsewhere") }] }));
  expect(moved).toMatchObject({ title: "Clickable" });
  expect(moved.snapshot as string).not.toContain("changes since");
  expect(moved.snapshot as string).toContain("[cursor=pointer]");
});

// 目的：只关心一个元素时截那个元素，图更小更清楚；潜在缺陷：截区没加滚动、跨进程 iframe 没加框架偏移而截到别处，
// 小图标按原尺寸截看不清，或元素已不在时截了整个视口。
test("captures a single element, enlarging small ones, also inside a cross-site iframe", async () => {
  const page = valueOf(await link("browser.navigate", 66, { url: `${origin}/frames` }));
  const sizeOf = (image: string) => app.evaluate(({ nativeImage }, data) => nativeImage.createFromDataURL(data).getSize(), image);
  const shot = async (ref: string) => sizeOf(valueOf(await link("browser.screenshot", 66, { ref })).image as string);
  // 小按钮（加上边距约 75×37）放大到较短边 160 像素，至多 4 倍；跨进程 iframe 在首屏之下，先滚入视野再截。
  const small = await shot(refOf(page.snapshot as string, "button", "In same"));
  expect(Math.min(small.width, small.height)).toBeGreaterThan(120);
  expect(small.width).toBeLessThan(1280);
  const inFrame = await shot(refOf(page.snapshot as string, "button", "Send"));
  expect(inFrame.width).toBeLessThan(1280);
  expect(await link("browser.screenshot", 66, { ref: "e9999" })).toMatchObject({ ok: false, code: "REF_STALE" });
});

// 目的：跨站 iframe 滚到眼前时，「只看视口」要列出它里面的元素；潜在缺陷：跨进程框架的布局在子会话里，可见区域没从
// 它的 iframe 元素推过去（或没加上框架自己的坐标系），框架明明在屏幕上，里面的内容却被当成看不见。
test("lists a cross-site iframe's contents in the viewport once it is scrolled into view", async () => {
  valueOf(await link("browser.navigate", 67, { url: `${origin}/frames` }));
  const viewport = async () => valueOf(await link("browser.snapshot", 67, { scope: "viewport" })).snapshot as string;
  const top = await viewport();
  expect(top).toContain("Top of the page");
  expect(top).not.toContain('textbox "Note"');

  valueOf(await link("browser.evaluate", 67, { expression: "document.getElementById('cross').scrollIntoView()" }));
  const scrolled = await viewport();
  expect(scrolled).toMatch(/- iframe "Cross"\n\s+- textbox "Note" \[ref=e\d+\]/);
  expect(scrolled).toContain('button "Send"');
  expect(scrolled).not.toContain("Top of the page");
});

// 目的：元素藏起来后再点，报的是「看不见」而不是别的；潜在缺陷：CDP 对没有布局的节点的报错文案变了，被当成普通失败。
test("reports a hidden element as not interactable", async () => {
  const page = valueOf(await link("browser.navigate", 68, { url: `${origin}/act` }));
  const button = refOf(page.snapshot as string, "button", "Search");
  valueOf(await link("browser.evaluate", 68, { expression: "document.querySelectorAll('button').forEach((b) => { if (b.textContent.includes('Search')) b.style.display = 'none'; })" }));
  const clicked = valueOf(await link("browser.act", 68, { actions: [{ type: "click", ref: button }] }));
  expect(clicked).toMatchObject({ completed: 0, failure: { code: "NOT_INTERACTABLE" } });
});

// 目的：截图上的坐标在手机模拟下也点得准；潜在缺陷：没写 viewport 声明的网页按 980 宽排版、缩小显示，截图是 980 宽，
// 越界检查却按模拟的 390 宽判断，照截图给的坐标被拒（实测）。
test("accepts screenshot coordinates on a page laid out wider than the emulated phone", async () => {
  valueOf(await link("browser.navigate", 69, { url: `${origin}/wide`, viewport: { width: 390, height: 844, mobile: true } }));
  const shot = valueOf(await link("browser.screenshot", 69, {}));
  const size = await app.evaluate(({ nativeImage }, data) => nativeImage.createFromDataURL(data).getSize(), shot.image as string);
  expect(size.width).toBe(980);
  const clicked = valueOf(await link("browser.act", 69, { actions: [{ type: "click", x: 700, y: 350 }] }));
  expect(clicked).toMatchObject({ failure: null, title: "hit" });
});

