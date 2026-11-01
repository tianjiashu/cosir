import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import type { AddressInfo } from "node:net";
import { _electron as electron, expect, test, type ElectronApplication } from "@playwright/test";

/** 与 `global-setup.mjs` 的打包位置一致。 */
const browserHarnessBundle = path.resolve(__dirname, "..", "..", "..", "..", "target", "electron-browser-harness", "main.cjs");

/**
 * 内置浏览器引擎在真实 Electron 里的行为（不经后端与链路）：快照、导航失败、JS 弹窗、非网页地址拦截、
 * 渲染进程崩溃恢复、标签归属与回收、UA。链路往返由 P3-1 的两端单测覆盖，工具由后端单测覆盖。
 */

const PAGES: Record<string, string> = {
  "/form": `<title>Form</title><h1>Sign up</h1><p>Fill in the form.</p><label>Email <input name="email" value="a@b.c"></label><button disabled>Send</button><a href="/second">Next</a>`,
  "/second": `<title>Second</title><p>Second page</p>`,
  "/dialog": `<title>Dialog</title><script>setTimeout(() => { document.title = "answered:" + confirm("Continue?"); }, 0)</script>`,
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

let server: http.Server;
let origin: string;
let app: ElectronApplication;

const LIMITS = { perTask: 2, total: 3 };

test.beforeAll(async () => {
  server = http.createServer((request, response) => {
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
  // Playwright 把每个 WebContentsView 当作页面接管，并在没有监听者时自己去关弹窗；弹窗已被引擎的
  // disableDialogs 关掉，它再关会报错并关闭整个应用连接。挂一个空监听让它不插手。
  app.on("window", (page) => page.on("dialog", () => undefined));
  await expect.poll(() => app.evaluate(() => Boolean((globalThis as { browserHarness?: unknown }).browserHarness))).toBe(true);
});

test.afterAll(async () => {
  await app?.close();
  server?.close();
});

type Page = { tab_id: string; url: string; title: string; snapshot: string; notes: string[]; other_tabs: string[] };
type Outcome = { ok: true; value: Page } | { ok: false; code: string; message: string };

/** 断言调用成功并取出页面结果。 */
function pageOf(outcome: Outcome): Page {
  if (!outcome.ok) throw new Error(`expected success, got ${outcome.code}: ${outcome.message}`);
  return outcome.value;
}

/** 在 main 进程里调用 `BrowserHost` 的方法，把 `HostMethodError` 转成可序列化的结果。 */
function call(method: "navigate" | "snapshot" | "act" | "screenshot" | "manageTabs", request: Record<string, unknown>): Promise<Outcome> {
  return app.evaluate(async (_electron, { method, request }) => {
    const { host } = (globalThis as unknown as { browserHarness: { host: Record<string, (...args: unknown[]) => Promise<unknown>> } }).browserHarness;
    try {
      return { ok: true as const, value: await host[method](request, new AbortController().signal) as unknown as Page };
    } catch (error) {
      const failure = error as { code?: string; message?: string };
      return { ok: false as const, code: failure.code ?? "UNKNOWN", message: failure.message ?? String(error) };
    }
  }, { method, request });
}

const navigate = (taskId: number, target: Record<string, unknown>, tab?: string) => call("navigate", { taskId, tab, target, limits: LIMITS });

test("opens a tab on first navigation and returns a snapshot with refs", async () => {
  const page = pageOf(await navigate(1, { url: `${origin}/form` }));
  expect(page).toMatchObject({ tab_id: "t1", title: "Form", other_tabs: [], notes: [] });
  expect(page.snapshot).toContain(`heading "Sign up" [level=1]`);
  expect(page.snapshot).toMatch(/textbox "Email" \[ref=e\d+\] value="a@b.c"/);
  expect(page.snapshot).toMatch(/button "Send" \[ref=e\d+\] \[disabled\]/);
  expect(page.snapshot).toMatch(/link "Next" \[ref=e\d+\]/);
});

test("supports history navigation within the task's current tab", async () => {
  await navigate(1, { url: `${origin}/second` });
  const back = await navigate(1, { action: "back" });
  expect(back).toMatchObject({ ok: true, value: { tab_id: "t1", title: "Form" } });
  const reload = await navigate(1, { action: "reload" });
  expect(reload).toMatchObject({ ok: true, value: { title: "Form" } });
});

test("reports load failures with the Chromium error", async () => {
  const outcome = await navigate(1, { url: "http://127.0.0.1:9/" });
  expect(outcome).toMatchObject({ ok: false, code: "NAVIGATION_FAILED" });
  expect(outcome.ok ? "" : outcome.message).toContain("ERR_UNSAFE_PORT");
});

// 目的：页面弹窗不能把标签卡住；潜在缺陷：confirm() 让页面与 Agent 一起永远等下去。
test("dismisses JavaScript dialogs and tells the model", async () => {
  const first = await navigate(1, { url: `${origin}/dialog` });
  const notes = [...pageOf(first).notes];
  await expect.poll(async () => {
    const page = pageOf(await call("snapshot", { taskId: 1 }));
    notes.push(...page.notes);
    return page.title;
  }).toBe("answered:false");
  expect(notes.some((note) => note.includes("confirm dialog") && note.includes("dismissed automatically"))).toBe(true);
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
    const { host } = (globalThis as unknown as { browserHarness: { host: { tabs: Map<string, { view: Electron.WebContentsView }> } } }).browserHarness;
    host.tabs.get("t1")!.view.webContents.forcefullyCrashRenderer();
  });
  await expect.poll(async () => {
    const outcome = await call("snapshot", { taskId: 1 });
    return outcome.ok ? "ok" : outcome.code;
  }).toBe("PAGE_CRASHED");
  expect(await navigate(1, { url: `${origin}/second` })).toMatchObject({ ok: true, value: { title: "Second" } });
});

// 目的：标签归属是硬边界、上限生效、删除后回收；潜在缺陷：子 Agent 操作父 Agent 的页面或标签无限增长。
test("keeps tabs per task, enforces limits and drops a task's tabs", async () => {
  const taskOneTab = pageOf(await navigate(1, { url: `${origin}/second` })).tab_id;
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
  const roomy = { perTask: 5, total: 50 };
  const first = pageOf(await call("navigate", { taskId: 6, target: { url: `${origin}/form` }, limits: roomy })).tab_id;
  const second = pageOf(await call("navigate", { taskId: 7, target: { url: `${origin}/second` }, limits: roomy })).tab_id;

  const placement = await app.evaluate((_electron, { first, second }) => {
    type Harness = {
      host: { place(tabId: string, rect: unknown): void; freeze(tabId: string): Promise<string>; tabs: Map<string, { view: Electron.WebContentsView }> };
      window: Electron.BrowserWindow;
    };
    const { host, window } = (globalThis as unknown as { browserHarness: Harness }).browserHarness;
    const base = window.contentView.children.length;
    const rect = { x: 10, y: 20, width: 300, height: 200 };
    host.place(first, rect);
    const withFirst = window.contentView.children.length;
    host.place(second, rect);
    const withSecond = window.contentView.children.length;
    const firstAttached = window.contentView.children.includes(host.tabs.get(first)!.view);
    const placed = host.tabs.get(second)!.view.getBounds();
    host.place(second, null);
    return {
      base,
      withFirst,
      withSecond,
      firstAttached,
      placed,
      after: window.contentView.children.length,
      detached: host.tabs.get(second)!.view.getBounds(),
    };
  }, { first, second });

  expect(placement.withFirst).toBe(placement.base + 1);
  expect(placement.withSecond).toBe(placement.base + 1);
  expect(placement.firstAttached).toBe(false);
  expect(placement.placed).toEqual({ x: 10, y: 20, width: 300, height: 200 });
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

const roomy = { perTask: 5, total: 50 };
type ActPage = Page & { completed: number; failure: { index: number; code: string; message: string } | null };

test("acts on the page through refs: select, wait, type and submit", async () => {
  const page = pageOf(await call("navigate", { taskId: 20, target: { url: `${origin}/act` }, limits: roomy }));
  const select = refOf(page.snapshot, "combobox", "Color");
  const load = refOf(page.snapshot, "button", "Load");
  const query = refOf(page.snapshot, "textbox", "Query");

  const first = pageOf(await call("act", { taskId: 20, actions: [
    { type: "select", ref: select, values: ["Blue"] },
    { type: "click", ref: load },
    { type: "wait_for", text: "loaded later", seconds: 5 },
  ] })) as ActPage;
  expect(first).toMatchObject({ completed: 3, failure: null, title: "color:b" });
  expect(first.snapshot).toContain("loaded later");

  const submitted = pageOf(await call("act", { taskId: 20, actions: [{ type: "type", ref: query, text: "cosir", clear: true, submit: true }] })) as ActPage;
  expect(submitted).toMatchObject({ completed: 1, failure: null, title: "Done" });
  expect(submitted.url).toBe(`${origin}/done?q=cosir`);

  // 目的：导航后旧 ref 一律失效；潜在缺陷：旧序号恰好指到新页面的另一个元素，点错东西。
  const stale = pageOf(await call("act", { taskId: 20, actions: [{ type: "click", ref: load }] })) as ActPage;
  expect(stale).toMatchObject({ completed: 0, failure: { index: 0, code: "REF_STALE" }, title: "Done" });
});

// 目的：长页面后半段的元素也能读到并操作，ref 跨快照不变；潜在缺陷：截断之后的内容对 Agent 永远不可见。
test("pages long snapshots with stable refs, then hovers, presses shortcuts and scrolls areas", async () => {
  const page = pageOf(await call("navigate", { taskId: 24, target: { url: `${origin}/rich` }, limits: roomy }));
  const note = page.snapshot.split("\n").at(-1)!;
  expect(note).toMatch(/read on with from_line=\d+$/);
  const menu = refOf(page.snapshot, "button", "Menu");
  const inside = refOf(page.snapshot, "button", "Inside");

  // 按截断提示一页页往下读，直到最后一页。
  let rest = page.snapshot;
  for (let next = rest.match(/read on with from_line=(\d+)$/); next; next = rest.match(/read on with from_line=(\d+)$/)) {
    rest = pageOf(await call("snapshot", { taskId: 24, fromLine: Number(next[1]) })).snapshot;
  }
  const last = refOf(rest, "link", "Item 799");
  expect(refOf(pageOf(await call("snapshot", { taskId: 24, fromLine: 1 })).snapshot, "button", "Menu")).toBe(menu);

  const acted = pageOf(await call("act", { taskId: 24, actions: [
    { type: "wait_for", textGone: "Loading", seconds: 5 },
    { type: "hover", ref: menu },
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

test("blocks file choosers and opens target=_blank links as tabs of the same task", async () => {
  const page = pageOf(await call("navigate", { taskId: 21, target: { url: `${origin}/act` }, limits: roomy }));
  const upload = refOf(page.snapshot, "button", "Upload");
  const popup = refOf(page.snapshot, "link", "Popup");
  const after = pageOf(await call("act", { taskId: 21, actions: [{ type: "click", ref: upload }, { type: "click", ref: popup }] }));
  await expect.poll(async () => {
    const tabs = await call("manageTabs", { taskId: 21, action: "list" });
    return tabs.ok ? (tabs.value as unknown as unknown[]).length : 0;
  }).toBe(2);
  const notes = [...after.notes, ...pageOf(await call("snapshot", { taskId: 21 })).notes].join("\n");
  expect(notes).toContain("file chooser");
  expect(notes).toMatch(/opened .*\/second in a new tab t\d+/);
});

// 目的：用户正在页面上操作时 Agent 不抢；潜在缺陷：Agent 的点击打断用户的登录输入。
test("refuses to act while the user is typing in the page", async () => {
  // 唯一的地址：按地址找网页视图，不能与别的用例的标签重名。
  const page = pageOf(await call("navigate", { taskId: 22, target: { url: `${origin}/act?typing` }, limits: roomy }));
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
  await call("navigate", { taskId: 23, target: { url: `${origin}/form` }, limits: roomy });
  const second = pageOf(await call("navigate", { taskId: 23, newTab: true, target: { url: `${origin}/second` }, limits: roomy }));

  const shot = await call("screenshot", { taskId: 23, fullPage: false });
  expect(shot.ok).toBe(true);
  const size = await app.evaluate(({ nativeImage }, image) => nativeImage.createFromDataURL(image).getSize(), (shot as unknown as { value: { image: string } }).value.image);
  expect(size).toEqual({ width: 1280, height: 800 });

  const listed = await call("manageTabs", { taskId: 23, action: "list" });
  expect(listed).toMatchObject({ ok: true, value: [{ current: false }, { tab_id: second.tab_id, current: true, title: "Second" }] });
  const first = (listed as unknown as { value: { tab_id: string }[] }).value[0].tab_id;
  expect(await call("manageTabs", { taskId: 23, action: "select", tab: first })).toMatchObject({ ok: true, value: [{ tab_id: first, current: true }, { current: false }] });
  expect(await call("manageTabs", { taskId: 23, action: "close", tab: first })).toMatchObject({ ok: true, value: [{ tab_id: second.tab_id, current: true }] });
});

