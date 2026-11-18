import { app, BrowserWindow, WebContentsView } from "electron";
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { BrowserHost } from "../../../electron/main/browser/browser-host";
import type { BrowserTab } from "../../../electron/main/browser/browser-tab";
import { configureBrowserSession, configureBrowserUserAgent } from "../../../electron/main/browser/browser-partition";
import { BrowserNetLogCapture } from "../../../electron/main/browser/diagnostics/netlog-capture";
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
  if (url.startsWith("/diagnostic-cookie")) { res.writeHead(200, { "Set-Cookie": "diagnostic_session=PRIVATE_COOKIE_VALUE; SameSite=Lax" }); res.end("ok"); return; }
  if (url.startsWith("/diagnostic-timeout")) { setTimeout(() => res.end("late"), 700); return; }
  if (url.startsWith("/diagnostic-cors")) { res.writeHead(403); res.end("CORS denied"); return; }
  if (url === "/settle-data") { setTimeout(() => res.end("DATA_ARRIVED_TEXT"), 1_200); return; }
  if (url === "/settle-hang") { setTimeout(() => res.end("late"), 10_000); return; }
  if (url === "/beacon") { req.resume(); setTimeout(() => { res.writeHead(204); res.end(); }, 400); return; }
  if (url === "/settle-page" || url === "/settle-hang-page" || url === "/settle-beacon-page") {
    const api = url === "/settle-hang-page" ? "/settle-hang" : "/settle-data";
    // 埋点页：每 150ms 一次上报、每次 400ms 才回 204，页面上始终有请求在路上。
    const beacons = url === "/settle-beacon-page" ? "setInterval(()=>fetch('/beacon',{method:'POST',body:'x'}),150);" : "";
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(`<title>Settle</title><p id="out">loading</p><script>${beacons}addEventListener('load',()=>fetch('${api}').then(r=>r.text()).then(t=>{document.getElementById('out').textContent=t}))</script>`);
    return;
  }
  if (url === "/settle-ticker") {
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(`<title>Ticker</title><p id="t">0</p><script>let n=0;setInterval(()=>{document.getElementById('t').textContent='tick '+(++n)},100)</script>`);
    return;
  }
  if (url === "/settle-batches") {
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(`<title>Batches</title><ul id="l"></ul><script>addEventListener('load',()=>[1,2,3].forEach(i=>setTimeout(()=>{let li=document.createElement('li');li.textContent='BATCH_'+i;document.getElementById('l').append(li)},i*200)))</script>`);
    return;
  }
  // Service Worker 回归：作用范围只到 /swscope/，不影响其他场景的页面。fetch 处理器几乎什么都不拦（与小红书一致），
  // 但不能是空函数：Chromium 认出空处理器会直接跳过 Worker，测不到缺陷。
  if (url === "/swscope/sw.js") { res.writeHead(200, { "Content-Type": "text/javascript" }); res.end("self.addEventListener('install',()=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(clients.claim()));self.addEventListener('fetch',e=>{if(e.request.url.endsWith('/swscope/never'))e.respondWith(new Response('x'))});"); return; }
  if (url.startsWith("/swscope/redirect-301")) { res.writeHead(301, { Location: "/swscope/page?via=301", "Cache-Control": "max-age=3600" }); res.end(); return; }
  if (url.startsWith("/swscope/redirect-302")) { res.writeHead(302, { Location: "/swscope/page?via=302" }); res.end(); return; }
  if (url.startsWith("/swscope/page")) {
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(`<title>SW scope</title><p>SW_SCOPE_PAGE</p><script>navigator.serviceWorker.register('/swscope/sw.js',{scope:'/swscope/'})</script>`);
    return;
  }
  // 落定 f（翻页）：加载时请求第一页；点「More」后再请求同一个接口的下一页，1 秒才回来。
  if (url.startsWith("/items?page=")) { const page = url.slice("/items?page=".length); setTimeout(() => res.end(`ITEM_${page}`), page === "1" ? 300 : 1_000); return; }
  if (url === "/items-page") {
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(`<title>Items</title><ul id="l"></ul><button onclick="more()">More</button><script>let n=0;function more(){n++;fetch('/items?page='+n).then(r=>r.text()).then(t=>{let li=document.createElement('li');li.textContent=t;document.getElementById('l').append(li)})}addEventListener('load',more)</script>`);
    return;
  }
  // 落定 g（后台轮询）：每 300ms 一次、每次 1.5 秒才回来的轮询；点「Detail」才请求真正的内容。
  if (url.startsWith("/poll?")) { setTimeout(() => res.end("{}"), 1_500); return; }
  if (url === "/detail") { setTimeout(() => res.end("DETAIL_OK"), 800); return; }
  if (url === "/poll-page") {
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(`<title>Poll</title><p id="d">none</p><button onclick="fetch('/detail').then(r=>r.text()).then(t=>{document.getElementById('d').textContent=t})">Detail</button><script>setInterval(()=>fetch('/poll?t='+Date.now()),300)</script>`);
    return;
  }
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
const click = (id: string) => ({ type: "click" as const, target: { ref: id }, button: "left" as const, clickCount: 1 as const, repeat: 1, modifiers: [] });
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
    await check("network diagnostics distinguish real XHR timeout and CORS preflight failure without logging credentials", async () => {
      const netLog = new BrowserNetLogCapture(() => undefined);
      netLog.start();
      await evaluate(parent, `Promise.all([
        fetch('/diagnostic-cookie?token=PRIVATE_QUERY_VALUE').then(() => fetch('/diagnostic-cookie')),
        fetch('http://localhost:${port}/diagnostic-cors', {headers:{'X-Diagnostic':'test'}}).catch(() => undefined),
        new Promise(resolve => {const xhr=new XMLHttpRequest();xhr.open('GET','/diagnostic-timeout');xhr.timeout=100;xhr.ontimeout=resolve;xhr.send();})
      ]).then(() => true)`);
      const dir = path.join(root, ".cosir/logs");
      const read = () => fs.readdirSync(dir).filter(name => /^browser-network-.*\.log$/.test(name))
        .flatMap(name => fs.readFileSync(path.join(dir, name), "utf8").trim().split("\n").map(line => JSON.parse(line)));
      await until(() => read().filter(line => line.data.method === "loadingFailed").length >= 2);
      const rows = read();
      assert.ok(!JSON.stringify(rows).includes("PRIVATE_"));
      const timeout = rows.find(line => line.data.url?.endsWith('/diagnostic-timeout') && line.data.method === 'requestWillBeSent').data;
      assert.ok(rows.some(line => line.data.request_id === timeout.request_id && line.data.error_text === 'net::ERR_ABORTED' && line.data.canceled));
      assert.ok(rows.some(line => line.data.cors_error === 'PreflightMissingAllowOriginHeader'));
      assert.ok(rows.some(line => line.data.initiator_type === 'preflight' && line.data.initiator_request_id));
      assert.ok(rows.some(line => line.data.method === 'responseReceivedExtraInfo' && line.data.status === 403));
      assert.ok(rows.some(line => line.data.set_cookie_names?.includes('diagnostic_session')));
      assert.ok(rows.some(line => line.event === 'browser_cookie_changed' && line.data.name === 'diagnostic_session' && line.data.removed === false));
      await netLog.stop();
      assert.equal(netLog.active, false);
      const capture = fs.readdirSync(dir).find(name => /^browser-netlog-.*\.json$/.test(name))!;
      const nativeLog = fs.readFileSync(path.join(dir, capture), 'utf8');
      assert.ok(JSON.parse(nativeLog).events.length > 0);
      assert.ok(!nativeLog.includes('PRIVATE_COOKIE_VALUE'));
    });
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
    await check("settling waits for content, not for beacons, rendering or a page that never holds still", async () => {
      const go = async (path: string, newTab = false) => {
        const started = Date.now();
        const result = await host.navigate({ sessionId: 7, context, newTab, target: { url: `${origin}${path}` } }, signal());
        return { result, ms: Date.now() - started, notes: result.notes.join("\n") };
      };
      // a) 内容在加载后 1.2 秒到达：等到它，不留备注。
      const data = await go("/settle-page", true);
      assert.ok(data.result.snapshot.includes("DATA_ARRIVED_TEXT"), data.result.snapshot);
      assert.ok(!/still (running|changing)/.test(data.notes), data.notes);
      // b) 持续埋点：只等内容接口，埋点不拖住、不进备注。
      const beacons = await go("/settle-beacon-page");
      assert.ok(beacons.result.snapshot.includes("DATA_ARRIVED_TEXT"), beacons.result.snapshot);
      assert.ok(!/still (running|changing)/.test(beacons.notes), beacons.notes);
      assert.ok(beacons.ms < 3_000, `beacon page took ${beacons.ms}ms`);
      // c) 没有请求、分几批渲染：等到最后一批。
      const batches = await go("/settle-batches");
      assert.ok(batches.result.snapshot.includes("BATCH_3"), batches.result.snapshot);
      // d) 一直在变：到上限交出，并说明是页面在变。
      const ticker = await go("/settle-ticker");
      assert.ok(ticker.notes.includes("the page was still changing") && ticker.notes.includes("wait_for"), ticker.notes);
      // e) 内容接口永远不回来：到上限交出，并列出它。
      const hanging = await go("/settle-hang-page");
      assert.ok(hanging.ms < 6_000, `took ${hanging.ms}ms`);
      assert.ok(hanging.notes.includes(`POST ${origin}/settle-hang`) || hanging.notes.includes(`GET ${origin}/settle-hang`), hanging.notes);
      assert.ok(hanging.notes.includes("may carry page content"), hanging.notes);
      host.userClose(hanging.result.tabId);
    });
    await check("settling waits for what an action caused: next pages, not the page's own polling", async () => {
      // f) 翻页：同一个接口每一步都请求一次，每次都要等到。
      const items = await host.navigate({ sessionId: 9, context, newTab: true, target: { url: `${origin}/items-page` } }, signal());
      assert.ok(items.snapshot.includes("ITEM_1"), items.snapshot);
      for (const page of ["ITEM_2", "ITEM_3"]) {
        const now = await host.snapshot({ sessionId: 9, tab: items.tabId, context, view: {} }, signal());
        const more = await host.act({ sessionId: 9, context, actions: [click(ref(now.snapshot, "More"))] }, signal());
        assert.ok(more.observation?.snapshot.includes(page), `${page} missing: ${more.observation?.snapshot}`);
        assert.ok(!more.observation?.notes.some((note) => note.includes("still running")), more.observation?.notes.join("\n"));
      }
      // g) 后台轮询：空闲超过宽限期后，轮询被认作页面自己发起；点击只等真正的内容，不被轮询拖满上限。
      const poll = await host.navigate({ sessionId: 9, context, newTab: false, target: { url: `${origin}/poll-page` } }, signal());
      await new Promise((resolve) => setTimeout(resolve, 4_500));
      const started = Date.now();
      const detail = await host.act({ sessionId: 9, context, actions: [click(ref(poll.snapshot, "Detail"))] }, signal());
      const ms = Date.now() - started;
      assert.ok(detail.observation?.snapshot.includes("DETAIL_OK"), detail.observation?.snapshot);
      assert.ok(!detail.observation?.notes.some((note) => note.includes("/poll")), detail.observation?.notes.join("\n"));
      assert.ok(ms < 2_600, `detail click took ${ms}ms`);
      host.userClose(items.tabId);
    });
    await check("a redirect into a stopped service worker's scope does not hang navigation", async () => {
      // Electron 44（Chromium 152）的 ServiceWorkerAutoPreload 缺陷（上游 559592113）：Worker 停止后，经跳转进入它作用范围的
      // 导航永远卡住（实测小红书直达搜索页）。用 CDP 直接停掉 Worker 复现「空闲约 30 秒被停」，不必真等。
      const opened = await host.navigate({ sessionId: 8, context, newTab: true, target: { url: `${origin}/swscope/page` } }, signal());
      const swTab = tab(opened.tabId);
      for (let i = 0; i < 50 && !(await evaluate<boolean>(swTab, "!!navigator.serviceWorker.controller")); i++) {
        if (i === 0) await host.navigate({ sessionId: 8, context, newTab: false, target: { url: `${origin}/swscope/page?again` } }, signal());
        await new Promise(r => setTimeout(r, 100));
      }
      assert.ok(await evaluate<boolean>(swTab, "!!navigator.serviceWorker.controller"), "service worker never took control");
      for (const via of ["301", "302"]) {
        await host.navigate({ sessionId: 8, context, newTab: false, target: { url: `${origin}/swscope/redirect-${via}?warm` } }, signal());
        await host.navigate({ sessionId: 8, context, newTab: false, target: { url: "about:blank" } }, signal());
        // stopAllWorkers 是异步的：确认 Worker 真的停了再导航，否则测的是「Worker 还在跑」，抓不到缺陷。
        let status = "unknown";
        const off = swTab.session.on<{ versions: { runningStatus: string }[] }>("ServiceWorker.workerVersionUpdated", ({ versions }) => { for (const v of versions) status = v.runningStatus; });
        await swTab.session.send("ServiceWorker.enable");
        await swTab.session.send("ServiceWorker.stopAllWorkers");
        await until(() => status === "stopped");
        off();
        await swTab.session.send("ServiceWorker.disable");
        const started = Date.now();
        const page = await host.navigate({ sessionId: 8, context, newTab: false, target: { url: `${origin}/swscope/redirect-${via}?after-stop` } }, signal());
        const ms = Date.now() - started;
        assert.ok(page.url.endsWith(`/swscope/page?via=${via}`), `${via}: landed on ${page.url}`);
        assert.ok(ms < 8_000, `${via}: navigation took ${ms}ms`);
      }
      host.userClose(opened.tabId);
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
    await check("cancel after first keyDown stops later characters and submit; late command blocks a new mutation", async () => {
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
