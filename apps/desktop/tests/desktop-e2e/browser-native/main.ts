import { app, BrowserWindow, WebContentsView } from "electron";
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { BrowserHost } from "../../../electron/main/browser/browser-host";
import type { BrowserTab } from "../../../electron/main/browser/browser-tab";
import { configureBrowserSession, configureBrowserUserAgent } from "../../../electron/main/browser/browser-partition";
import { rendererSession } from "../../../electron/main/window";

/** 独立原生 Electron 验收：没有 Playwright、远程调试端口或第二个页面接管器。只访问本机 HTTP。 */
const root = process.env.CODING_AGENT_DATA_DIR!;
app.setName("Cosir");
app.setPath("userData", path.join(root, "user-data"));
configureBrowserUserAgent();
app.on("window-all-closed", () => undefined);
const watchdog = setTimeout(() => app.exit(2), 90_000);
const passed: string[] = [];
const headers = new Map<string, string>();
const hits = new Map<string, number>();
let host: BrowserHost;
let window: BrowserWindow;
const server = http.createServer((req, res) => {
  const url = req.url!;
  hits.set(url, (hits.get(url) ?? 0) + 1);
  headers.set(url, req.headers["user-agent"]!);
  if (url === "/redirect") { res.writeHead(302, { Location: "/redirected" }); res.end(); return; }
  if (url.startsWith("/asset")) { res.writeHead(200, { "Cache-Control": "public, max-age=31536000, immutable" }); res.end(String(hits.get(url))); return; }
  if (url === "/worker.js") { res.writeHead(200, { "Content-Type": "text/javascript" }); res.end("postMessage(navigator.userAgent)"); return; }
  res.writeHead(200, { "Content-Type": "text/html" });
  res.end(`<title>Native acceptance</title><style>body{margin:8px}button,input{display:block;margin:12px}</style>
    <script>window.initialUA=navigator.userAgent;window.count=0;window.submits=0;</script>
    <form onsubmit="event.preventDefault();window.submits++"><input aria-label="Text"><button type="submit">Submit</button></form>
    <button onclick="window.count++">Count</button><button onclick="confirm('Continue?');window.count++">Confirm</button>
    <button onclick="window.close()">Close</button><button disabled>Disabled</button><input aria-label="Readonly" readonly>
    <button id="covered" onclick="window.count++">Covered</button>
    <script>let r=document.getElementById('covered').getBoundingClientRect();let cover=document.createElement('div');cover.style.cssText='position:absolute;background:black;z-index:9;left:'+r.x+'px;top:'+r.y+'px;width:'+r.width+'px;height:'+r.height+'px';document.body.append(cover);</script>`);
});
const context = { limits: { perSession: 20, total: 30 }, downloadDir: path.join(root, "downloads") };
const signal = () => new AbortController().signal;
const tab = (id: string): BrowserTab => (host as unknown as { tabs: Map<string, { tab: BrowserTab }> }).tabs.get(id)!.tab;
const evaluate = async <T>(target: BrowserTab, expression: string): Promise<T> => {
  const result = await target.session.send<{ result: { value: T }; exceptionDetails?: unknown }>("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  assert.equal(result.exceptionDetails, undefined);
  return result.result.value;
};
const ref = (snapshot: string, label: string) => {
  const line = snapshot.split("\n").find((line) => line.includes(`"${label}"`) && line.includes("[ref="));
  assert.ok(line, `${label} not in ${snapshot}`);
  return /\[ref=(e\d+)\]/.exec(line)![1];
};
const click = (id: string) => ({ type: "click" as const, target: { ref: id }, button: "left" as const, clickCount: 1 as const, modifiers: [] });
const until = async (check: () => boolean) => { const end = Date.now() + 5_000; while (!check()) { assert.ok(Date.now() < end, "condition timed out"); await new Promise(r => setTimeout(r, 20)); } };
async function check(name: string, run: () => Promise<void>) { await run(); passed.push(name); console.log(`PASS ${name}`); }

void app.whenReady().then(async () => {
  app.dock?.hide();
  await new Promise<void>(resolve => server.listen(0, resolve));
  const port = (server.address() as { port: number }).port;
  const origin = `http://127.0.0.1:${port}`;
  window = new BrowserWindow({ show: false });
  host = new BrowserHost(() => window);
  configureBrowserSession(host);
  try {
    const page = await host.navigate({ sessionId: 1, context, newTab: true, target: { url: origin } }, signal());
    const parent = tab(page.tabId);
    await check("default UA covers first request, popup, redirect, iframe and worker", async () => {
      const ua = await evaluate<string>(parent, "window.initialUA");
      assert.equal(headers.get("/"), ua); assert.ok(!ua.includes(" Electron/"));
      await evaluate(parent, "window.open('/popup'); true");
      await until(() => host.state().tabs.length === 2 && tab("t2").initialized);
      assert.equal(headers.get("/popup"), ua); assert.equal(await evaluate(tab("t2"), "window.initialUA"), ua);
      await evaluate(parent, "let a=document.createElement('a');a.href='/blank';a.target='_blank';document.body.append(a);a.click();true");
      await until(() => host.state().tabs.length === 3 && tab("t3").initialized);
      assert.equal(headers.get("/blank"), ua); assert.equal(await evaluate(tab("t3"), "window.initialUA"), ua);
      for (const src of [origin + "/frame", `http://localhost:${port}/cross-frame`]) {
        await evaluate(parent, `new Promise(r=>{let f=document.createElement('iframe');f.src=${JSON.stringify(src)};f.onload=()=>r(true);document.body.append(f)})`);
      }
      assert.equal(headers.get("/frame"), ua); assert.equal(headers.get("/cross-frame"), ua);
      assert.equal(await evaluate(parent, "new Promise(r=>{let w=new Worker('/worker.js');w.onmessage=e=>{w.terminate();r(e.data)}})"), ua);
      assert.equal(headers.get("/worker.js"), ua);
      await host.navigate({ sessionId: 2, context, newTab: true, target: { url: origin + "/redirect" } }, signal());
      assert.equal(headers.get("/redirect"), ua); assert.equal(headers.get("/redirected"), ua);
    });
    await check("only renderer Session disables immutable cache; website cookies persist across tabs", async () => {
      await evaluate(parent, "fetch('/asset-site').then(()=>fetch('/asset-site')).then(()=>true)");
      assert.equal(hits.get("/asset-site"), 1);
      const view = new WebContentsView({ webPreferences: { session: rendererSession(), sandbox: true } });
      await view.webContents.loadURL(origin + "/app");
      await view.webContents.executeJavaScript("fetch('/asset-app').then(()=>fetch('/asset-app')).then(()=>true)");
      assert.equal(hits.get("/asset-app"), 2);
      await view.webContents.reload();
      await new Promise(r => setTimeout(r, 150));
      await view.webContents.executeJavaScript("fetch('/asset-app').then(()=>true)");
      assert.equal(hits.get("/asset-app"), 3);
      view.webContents.close();
      await parent.contents.session.cookies.set({ url: origin, name: "acceptance", value: "retained", expirationDate: Date.now() / 1000 + 3600 });
      const other = await host.navigate({ sessionId: 3, context, newTab: true, target: { url: origin } }, signal());
      assert.ok((await evaluate<string>(tab(other.tabId), "document.cookie")).includes("acceptance=retained"));
    });
    await check("real Vite dependency rebuild reloads fresh code in the same renderer Session", async () => {
      const view = new WebContentsView({ webPreferences: { session: rendererSession(), sandbox: true } });
      try {
        await view.webContents.loadURL(process.env.COSIR_RENDERER_URL!);
        await until(() => view.webContents.getTitle() === "dependency-v1");
        // 先卸载旧 Vite client，避免其自动重连 reload 与显式导航竞争；Session 和视图都保留。
        await view.webContents.loadURL("about:blank");
        fs.writeFileSync(path.join(root, "rebuild-request"), "rebuild");
        await until(() => fs.existsSync(path.join(root, "rebuild-ready")));
        await view.webContents.loadURL(process.env.COSIR_RENDERER_URL!);
        await until(() => view.webContents.getTitle() === "dependency-v2");
      } finally { view.webContents.close(); }
    });
    await check("real click survives a failed observation", async () => {
      const snapshot = await host.snapshot({ sessionId: 1, tab: parent.tabId, context, view: {} }, signal());
      const saved = parent.snapshot;
      parent.snapshot = async () => { throw new Error("injected observation failure"); };
      try {
        const result = await host.act({ sessionId: 1, tab: parent.tabId, context, actions: [click(ref(snapshot.snapshot, "Count"))] }, signal());
        assert.equal(result.execution.steps[0].status, "done"); assert.equal(result.observation, null);
        assert.match(result.observationError!.message, /injected/); assert.equal(await evaluate(parent, "window.count"), 1);
      } finally { parent.snapshot = saved; }
    });
    await check("cancel after first keyDown stops later characters and submit; late command blocks handoff and new mutation", async () => {
      const snapshot = await host.snapshot({ sessionId: 1, tab: parent.tabId, context, view: {} }, signal());
      const cancel = new AbortController();
      const debuggerApi = parent.contents.debugger;
      const send = debuggerApi.sendCommand.bind(debuggerApi);
      let release!: () => void;
      const pending = new Promise<void>(resolve => { release = resolve; });
      let keydowns = 0;
      debuggerApi.sendCommand = async (method, params, sessionId) => {
        const result = await send(method, params, sessionId);
        if (method === "Input.dispatchKeyEvent" && params.type === "keyDown") {
          keydowns++;
          if (params.key === "a") { cancel.abort(new Error("cancel first character")); await pending; }
        }
        return result;
      };
      try {
        const acting = host.act({ sessionId: 1, tab: parent.tabId, context, actions: [{ type: "type", ref: ref(snapshot.snapshot, "Text"), text: "abc", clear: true, submit: true, keyByKey: true }] }, cancel.signal);
        await until(() => cancel.signal.aborted);
        const listed = host.manageTabs({ sessionId: 1, context, action: "list" }).find(t => t.tabId === parent.tabId)!;
        assert.notEqual(listed.operation, null);
        await assert.rejects(host.evaluate({ sessionId: 1, context, tab: parent.tabId, expression: "window.count++" }, signal()), /not settled/);
        await acting;
        assert.equal(parent.operation!.state, "unknown");
        release(); await until(() => parent.operation === null);
        assert.equal(keydowns, 1); assert.equal(await evaluate(parent, "document.querySelector('input').value"), "a");
        assert.equal(await evaluate(parent, "window.submits"), 0);
      } finally { release(); debuggerApi.sendCommand = send; }
    });
    await check("a real dialog interrupts input and explicit dialog control releases the operation", async () => {
      const snapshot = await host.snapshot({ sessionId: 1, tab: parent.tabId, context, view: {} }, signal());
      const result = await host.act({ sessionId: 1, tab: parent.tabId, context, actions: [click(ref(snapshot.snapshot, "Confirm")), click(ref(snapshot.snapshot, "Count"))] }, signal());
      assert.equal(result.execution.steps[0].status, "unknown");
      assert.equal(result.execution.steps[1].status, "skipped");
      const dialog = parent.dialog!; assert.ok(dialog);
      const answered = await host.act({ sessionId: 1, tab: parent.tabId, context, actions: [{ type: "dialog", id: dialog.id, accept: true }] }, signal());
      assert.equal(answered.execution.steps[0].status, "done");
      await until(() => parent.operation === null);
      assert.equal(await evaluate(parent, "window.count"), 2);
    });
    await check("handoff constraints use the actual target; read-only access remains available", async () => {
      const handed = { sessionId: 1, tab: parent.tabId, context, handoffTabs: [parent.tabId] };
      const snapshot = await host.snapshot({ ...handed, view: {} }, signal());
      for (const request of [
        () => host.act({ ...handed, actions: [click(ref(snapshot.snapshot, "Count"))] }, signal()),
        () => host.evaluate({ ...handed, expression: "window.count++" }, signal()),
        () => host.navigate({ ...handed, newTab: false, target: { url: origin } }, signal()),
        () => host.screenshot({ ...handed, fullPage: false, ref: ref(snapshot.snapshot, "Count") }, signal()),
      ]) await assert.rejects(request(), /handed to the user/);
      assert.throws(() => host.manageTabs({ ...handed, action: "close" }), /handed to the user/);
      host.manageTabs({ ...handed, action: "select" });
      await assert.rejects(host.evaluate({ ...handed, tab: undefined, expression: "1" }, signal()), /handed to the user/);
    });
    await check("covered, disabled and readonly targets fail before business input", async () => {
      const snapshot = await host.snapshot({ sessionId: 1, tab: parent.tabId, context, view: {} }, signal());
      for (const label of ["Covered", "Disabled"]) {
        const result = await host.act({ sessionId: 1, tab: parent.tabId, context, actions: [click(ref(snapshot.snapshot, label))] }, signal());
        assert.equal(result.execution.steps[0].status, "failed"); assert.equal(result.execution.steps[0].error!.code, "NOT_INTERACTABLE");
      }
      const result = await host.act({ sessionId: 1, tab: parent.tabId, context, actions: [{ type: "type", ref: ref(snapshot.snapshot, "Readonly"), text: "x", clear: true, submit: false, keyByKey: false }] }, signal());
      assert.equal(result.execution.steps[0].status, "failed"); assert.equal(await evaluate(parent, "document.querySelector('[readonly]').value"), "");
    });
    await check("cancelled drag releases once and sends no moves or drop after cancellation", async () => {
      const cancel = new AbortController();
      const debuggerApi = parent.contents.debugger;
      const send = debuggerApi.sendCommand.bind(debuggerApi);
      const calls: { method: string; params: Record<string, unknown> }[] = [];
      let release!: () => void;
      const pending = new Promise<void>(resolve => { release = resolve; });
      debuggerApi.sendCommand = async (method, params, sessionId) => {
        calls.push({ method, params });
        const result = await send(method, params, sessionId);
        if (method === "Input.dispatchMouseEvent" && params.type === "mousePressed") {
          cancel.abort(new Error("cancel drag")); await pending;
        }
        return result;
      };
      try {
        const result = await host.act({ sessionId: 1, tab: parent.tabId, context, actions: [{ type: "drag", start: { x: 500, y: 500 }, end: { x: 600, y: 500 } }] }, cancel.signal);
        assert.equal(result.execution.steps[0].status, "unknown");
        assert.ok(calls.some(call => call.method === "Input.setInterceptDrags" && call.params.enabled === false));
        release(); await until(() => parent.operation === null);
        assert.equal(calls.filter(call => call.params.type === "mouseReleased").length, 1);
        assert.equal(calls.filter(call => call.params.buttons === 1 || call.method === "Input.dispatchDragEvent").length, 0);
      } finally { release(); debuggerApi.sendCommand = send; }
    });
    await check("moving targets fail before input; contenteditable accepts non-Latin text", async () => {
      await evaluate(parent, `let moving=document.createElement('button');moving.id='moving';moving.textContent='Moving';moving.style.cssText='position:absolute;top:500px;left:100px;animation:move 2s infinite linear';moving.onclick=()=>window.count++;document.body.append(moving);let style=document.createElement('style');style.textContent='@keyframes move {to{transform:translateX(200px)}}';document.head.append(style);let edit=document.createElement('div');edit.contentEditable='true';edit.role='textbox';edit.setAttribute('aria-label','Editable');edit.style.cssText='position:absolute;top:600px;width:300px;height:40px;border:1px solid';document.body.append(edit);true`);
      const snapshot = await host.snapshot({ sessionId: 1, tab: parent.tabId, context, view: {} }, signal());
      const moving = await host.act({ sessionId: 1, tab: parent.tabId, context, actions: [click(ref(snapshot.snapshot, "Moving"))] }, signal());
      assert.equal(moving.execution.steps[0].status, "failed");
      assert.equal(moving.execution.steps[0].error!.code, "NOT_INTERACTABLE");
      const typed = await host.act({ sessionId: 1, tab: parent.tabId, context, actions: [{ type: "type", ref: ref(snapshot.snapshot, "Editable"), text: "中文输入🙂", clear: true, submit: false, keyByKey: false }] }, signal());
      assert.equal(typed.execution.steps[0].status, "done");
      assert.equal(await evaluate(parent, "document.querySelector('[contenteditable]').textContent"), "中文输入🙂");
    });
    await check("self-closing popup retains execution receipt", async () => {
      const popup = tab("t2");
      const snapshot = await host.snapshot({ sessionId: 1, tab: popup.tabId, context, view: {} }, signal());
      const result = await host.act({ sessionId: 1, tab: popup.tabId, context, actions: [click(ref(snapshot.snapshot, "Close"))] }, signal());
      assert.equal(result.execution.steps.length, 1); assert.notEqual(result.execution.steps[0].status, "skipped");
      assert.equal(result.observation, null); assert.ok(!host.state().tabs.some(t => t.tabId === popup.tabId));
    });
    fs.writeFileSync(path.join(root, "result.json"), JSON.stringify({ passed }, null, 2));
    console.log(`NATIVE ACCEPTANCE: ${passed.length} passed`);
    app.exit(0);
  } catch (error) {
    fs.writeFileSync(path.join(root, "result.json"), JSON.stringify({ passed, error: String(error), stack: (error as Error).stack }, null, 2));
    console.error(error); app.exit(1);
  } finally {
    clearTimeout(watchdog); host?.closeAll(); window?.destroy(); server.close();
  }
});
