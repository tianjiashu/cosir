import fs from "node:fs";
import path from "node:path";
import http from "node:http";
import { expect, test } from "@playwright/test";
import {
  backendBaseUrl,
  capabilityFor,
  captureCapability,
  desktopFetch,
  launchDesktop,
  unstartedSession,
  waitForAppShell,
} from "./desktop-app";

/** 真实 Chromium 请求钩子和 ASGI 边界：SSE 由桌面冒烟覆盖，此处覆盖 WS、frame 与重定向。 */
test("keeps local control identity in the main frame and supports real terminal WebSocket input", async () => {
  const { app, page, home } = await launchDesktop();
  let backend = "";
  let capturedCapability = false;
  let capturedRequests = 0;
  let terminalUrl: string | null = null;
  const server = http.createServer((request, response) => {
    if (request.url === "/redirect") { response.writeHead(302, { Location: `${backend}/workspaces` }).end(); return; }
    capturedRequests += 1;
    capturedCapability ||= Boolean(request.headers["x-cosir-capability"]);
    response.writeHead(200, { "Access-Control-Allow-Origin": "*", "Content-Type": "text/html" }).end("ok");
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const external = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  try {
    await waitForAppShell(page);
    backend = await backendBaseUrl(page);
    expect(await page.evaluate((base) => fetch(`${base}/workspaces`).then((r) => r.status), backend)).toBe(200);
    const iframe = await page.evaluate((base) => new Promise<string>((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("iframe response timed out")), 10_000);
      const frame = document.createElement("iframe");
      window.addEventListener("message", (event) => { if (event.source === frame.contentWindow) { clearTimeout(timeout); frame.remove(); resolve(String(event.data)); } });
      frame.srcdoc = `<script>fetch(${JSON.stringify(`${base}/workspaces`)}).then(r=>parent.postMessage(String(r.status),'*')).catch(()=>parent.postMessage('blocked','*'))</script>`;
      document.body.appendChild(frame);
    }), backend);
    expect(iframe).toBe("blocked");
    expect(await page.evaluate((url) => fetch(url).then((r) => String(r.status)).catch(() => "blocked"), `${external}/redirect`)).toBe("blocked");
    // 起点有身份，离开后端后身份必须剥离；使用浏览器响应替身只产生重定向，不替换请求钩子。
    await page.route(`${backend}/identity-test-redirect`, (route) => route.fulfill({ status: 302, headers: { Location: external } }));
    await page.evaluate((base) => fetch(`${base}/identity-test-redirect`).catch(() => null), backend);
    await page.unroute(`${backend}/identity-test-redirect`);
    expect(capturedRequests).toBeGreaterThan(0);
    expect(capturedCapability).toBe(false);

    const workspacePath = path.join(home, "workspace"); fs.mkdirSync(workspacePath);
    const workspace = await (await desktopFetch(`${backend}/workspaces`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "WS 验收", root_path: workspacePath }) })).json();
    const session = await unstartedSession(backend, workspace.workspace_id);
    const terminalResponse = await desktopFetch(`${backend}/sessions/${session}/terminal/sessions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ shell: "auto" }) });
    expect(terminalResponse.status, await terminalResponse.clone().text()).toBe(200);
    const terminal = (await terminalResponse.json()).terminalSessionId;
    terminalUrl = `${backend}/sessions/${session}/terminal/sessions/${terminal}`;
    const url = `${backend.replace("http:", "ws:")}/sessions/${session}/terminal/sessions/${terminal}/stream`;
    const denied = await new Promise<number>((resolve, reject) => {
      const request = http.request(url.replace("ws:", "http:"), { headers: {
        Connection: "Upgrade", Upgrade: "websocket", "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": Buffer.alloc(16, 7).toString("base64"),
      } }, (response) => { response.resume(); resolve(response.statusCode!); });
      request.setTimeout(10_000, () => request.destroy(new Error("untrusted WS timed out")));
      request.on("upgrade", (_response, socket) => { socket.destroy(); reject(new Error("untrusted WebSocket was accepted")); });
      request.on("error", reject);
      request.end();
    });
    expect(denied).toBe(403);
    const marker = path.join(home, "ws-executed.txt");
    const command = process.platform === "win32"
      ? `echo real-websocket-verified > "${marker}"\r`
      : `printf real-websocket-verified > '${marker}'\r`;
    const output = await page.evaluate(({ url, command }) => new Promise<string>((resolve, reject) => {
      const socket = new window.WebSocket(url);
      let text = "";
      const timeout = setTimeout(() => { socket.close(); reject(new Error("terminal WebSocket timed out")); }, 15_000);
      socket.onopen = () => socket.send(JSON.stringify({ type: "attach", after_seq: null }));
      socket.onerror = () => { clearTimeout(timeout); reject(new Error("trusted WebSocket rejected")); };
      socket.onmessage = (event) => {
        const frame = JSON.parse(String(event.data));
        if (frame.type === "attached") socket.send(JSON.stringify({ type: "input", data_base64: btoa(command) }));
        if (frame.type === "output") {
          text += atob(frame.data_base64);
          if (text.includes("real-websocket-verified")) { clearTimeout(timeout); socket.close(); resolve(text); }
        }
      };
    }), { url, command });
    expect(output).toContain("real-websocket-verified");
    await expect.poll(() => fs.existsSync(marker)).toBe(true);
    expect(fs.readFileSync(marker, "utf8")).toContain("real-websocket-verified");
    expect((await desktopFetch(`${backend}/sessions/${session}/terminal/sessions/${terminal}`, { method: "DELETE" })).status).toBe(200);
  } finally {
    if (terminalUrl) await desktopFetch(terminalUrl, { method: "DELETE" });
    await app.close();
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

/** 宿主每次拉起后端都重新发一份身份：重启后旧凭据必须失效，而页面无需任何动作即可继续通信。 */
test("reissues the local control identity when the backend restarts", async () => {
  const { app, page } = await launchDesktop();
  try {
    await waitForAppShell(page);
    const before = await backendBaseUrl(page);
    const beforeToken = capabilityFor(before);
    expect(beforeToken).toBeTruthy();
    expect(await page.evaluate((base) => fetch(`${base}/workspaces`).then((r) => r.status), before)).toBe(200);
    const generation = (await page.evaluate(() => window.cosirHost!.backend.snapshot())).generation;

    await page.evaluate(() => window.cosirHost!.backend.restart());
    // 换代判据用 generation 而不是地址：新后端可能恰好复用了旧端口。
    await expect
      .poll(
        async () => {
          const snapshot = await page.evaluate(() => window.cosirHost!.backend.snapshot());
          return snapshot.status.state === "ready" && snapshot.generation > generation;
        },
        { timeout: 120_000, intervals: [500] },
      )
      .toBe(true);
    const restarted = await page.evaluate(() => window.cosirHost!.backend.snapshot());
    const after = restarted.backendBaseUrl!;
    const afterToken = await captureCapability(page, after);

    expect(afterToken).not.toBe(beforeToken);
    // 新的一代由宿主自动注入新身份，页面不用做任何事就继续能通信。
    expect(await page.evaluate((base) => fetch(`${base}/workspaces`).then((r) => r.status), after)).toBe(200);

    // 直连进程不经过宿主钩子：旧凭据与不带凭据都必须被拒绝。
    const withOldToken = await fetch(`${after}/workspaces`, {
      headers: { "X-Cosir-Capability": beforeToken! },
    });
    expect(withOldToken.status).toBe(403);
    expect((await fetch(`${after}/workspaces`)).status).toBe(403);
  } finally {
    await app.close();
  }
});

/** 宿主在界面会话里拦下的公网请求（`renderer_outbound_blocked`），读自临时数据根下的桌面日志。 */
function blockedOrigins(home: string): string[] {
  const logDir = path.join(home, ".cosir", "logs");
  if (!fs.existsSync(logDir)) return [];
  return fs.readdirSync(logDir)
    .filter((name) => name.startsWith("desktop-") && name.endsWith(".log"))
    .flatMap((name) => fs.readFileSync(path.join(logDir, name), "utf8").split("\n"))
    .filter(Boolean)
    .map((line) => JSON.parse(line) as { event?: string; data?: { origin?: string } })
    .filter((line) => line.event === "renderer_outbound_blocked")
    .map((line) => String(line.data?.origin));
}

/**
 * 界面渲染模型写的内容不联网：宿主钩子拦下界面会话的一切公网请求。目的：模型挑的主机名与参数一显示就外传；
 * 潜在缺陷：拦截误伤界面自身的资源（启动到外壳出现时不应有任何拦截记录）。被拦的请求在发出前取消，不真的联网。
 */
test("keeps the app's pages off the internet without blocking the app itself", async () => {
  const { app, page, home } = await launchDesktop();
  try {
    await waitForAppShell(page);
    expect(blockedOrigins(home)).toEqual([]);
    const outcome = await page.evaluate(async () => {
      const image = await new Promise<string>((resolve) => {
        const probe = new Image();
        probe.onload = () => resolve("loaded");
        probe.onerror = () => resolve("blocked");
        probe.src = "https://egress-probe.invalid/favicon.ico?d=secret";
      });
      const request = await fetch("https://egress-probe-fetch.invalid/").then(() => "sent", () => "blocked");
      return { image, request };
    });
    expect(outcome).toEqual({ image: "blocked", request: "blocked" });
    await expect.poll(() => blockedOrigins(home)).toEqual(expect.arrayContaining([
      "https://egress-probe.invalid", "https://egress-probe-fetch.invalid",
    ]));
  } finally {
    await app.close();
  }
});
