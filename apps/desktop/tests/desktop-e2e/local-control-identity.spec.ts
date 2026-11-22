import fs from "node:fs";
import path from "node:path";
import http from "node:http";
import { expect, test } from "@playwright/test";
import { backendBaseUrl, desktopFetch, launchDesktop, unstartedSession, waitForAppShell } from "./desktop-app";

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
