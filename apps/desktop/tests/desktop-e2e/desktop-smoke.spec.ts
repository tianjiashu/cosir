import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";

const desktopRoot = path.resolve(__dirname, "..", "..");
const repositoryRoot = path.resolve(desktopRoot, "..", "..");
const backendPython = path.join(repositoryRoot, "apps", "backend", process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");

/**
 * 启动真实宿主。数据根指向临时目录（`CODING_AGENT_DATA_DIR`）：SQLite、日志与运行期文件全部隔离，不碰开发者的真实数据。
 * 已有后端虚拟环境时直接用它的解释器，省去在全新 uv 缓存里重新同步依赖。
 */
async function launchDesktop(): Promise<{ app: ElectronApplication; page: Page; home: string }> {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "cosir-desktop-e2e-"));
  const app = await electron.launch({
    args: [desktopRoot],
    env: {
      ...process.env,
      CODING_AGENT_DATA_DIR: home,
      ...(fs.existsSync(backendPython) ? { COSIR_BACKEND_PYTHON: backendPython } : {}),
    } as Record<string, string>,
  });
  const page = await app.firstWindow();
  return { app, page, home };
}

async function backendBaseUrl(page: Page): Promise<string> {
  const snapshot = await page.evaluate(() => window.cosirHost!.backend.snapshot());
  expect(snapshot.status).toEqual({ state: "ready" });
  return snapshot.backendBaseUrl!;
}

test("starts the host, reaches the workspace shell and isolates its data under the given home", async () => {
  const { app, page, home } = await launchDesktop();
  try {
    await expect(page).toHaveTitle("Cosir");
    await expect(page.getByRole("navigation", { name: "工作区任务列表" })).toBeVisible({ timeout: 120_000 });
    await expect(page.getByText("无法连接本机后端")).toHaveCount(0);
    expect(await backendBaseUrl(page)).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
    expect(fs.existsSync(path.join(home, ".cosir", "storage"))).toBe(true);
  } finally {
    await app.close();
  }
});

/** 读出临时数据根下全部后端结构化日志行里的事件名（含同日大小分片）。 */
function backendLogEvents(home: string): string[] {
  const logDir = path.join(home, ".cosir", "logs");
  if (!fs.existsSync(logDir)) return [];
  return fs.readdirSync(logDir)
    .filter((name) => name.startsWith("backend-") && !name.startsWith("backend-console") && name.endsWith(".log"))
    .flatMap((name) => fs.readFileSync(path.join(logDir, name), "utf8").split("\n"))
    .filter(Boolean)
    .map((line) => {
      try {
        return String((JSON.parse(line) as { event?: unknown }).event ?? "");
      } catch {
        return "";
      }
    });
}

// 宿主工具链路：宿主用注入后端的同一个 token 连上 `/host/link`；token 不一致时后端只会记 host_link_rejected。
test("the host connects its tool link to the backend", async () => {
  const { app, page, home } = await launchDesktop();
  try {
    await expect(page.getByRole("navigation", { name: "工作区任务列表" })).toBeVisible({ timeout: 120_000 });
    await expect.poll(() => backendLogEvents(home).includes("host_link_connected"), { timeout: 15_000 }).toBe(true);
    expect(backendLogEvents(home)).not.toContain("host_link_rejected");
  } finally {
    await app.close();
  }
});

test("closing the main window only hides it; the backend keeps running", async () => {
  const { app, page } = await launchDesktop();
  try {
    await expect(page.getByRole("navigation", { name: "工作区任务列表" })).toBeVisible({ timeout: 120_000 });
    const baseUrl = await backendBaseUrl(page);

    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());

    await expect.poll(() => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().map((item) => item.isVisible()))).toEqual([false]);
    expect((await fetch(`${baseUrl}/health`)).status).toBe(200);
  } finally {
    await app.close();
  }
});

test("quitting stops the backend process tree", async () => {
  const { app, page } = await launchDesktop();
  await expect(page.getByRole("navigation", { name: "工作区任务列表" })).toBeVisible({ timeout: 120_000 });
  const baseUrl = await backendBaseUrl(page);

  await app.evaluate(({ app: electronApp }) => electronApp.quit());
  await app.waitForEvent("close", { timeout: 30_000 });

  await expect.poll(async () => {
    try {
      await fetch(`${baseUrl}/health`, { signal: AbortSignal.timeout(500) });
      return "alive";
    } catch {
      return "stopped";
    }
  }, { timeout: 10_000 }).toBe("stopped");
});

test("opens and reloads a conversation through the real snapshot HTTP route", async () => {
  const { app, page, home } = await launchDesktop();
  try {
    await expect(page.getByRole("navigation", { name: "工作区任务列表" })).toBeVisible({ timeout: 120_000 });
    const base = await backendBaseUrl(page);
    const workspacePath = path.join(home, "workspace");
    fs.mkdirSync(workspacePath);
    const workspaceResponse = await fetch(`${base}/workspaces`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "HTTP 回归", root_path: workspacePath }),
    });
    expect(workspaceResponse.status).toBe(200);
    const workspace = await workspaceResponse.json() as { workspace_id: number };
    const taskResponse = await fetch(`${base}/workspaces/${workspace.workspace_id}/tasks`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: "快照序列化回归", creationCommandId: "snapshot-regression" }),
    });
    expect(taskResponse.status).toBe(200);
    const task = await taskResponse.json() as { task_id: number };
    const response = await fetch(`${base}/tasks/${task.task_id}/assistant/state`);
    expect(response.status).toBe(200);
    expect((await response.json() as { messages: unknown[] }).messages).toEqual([]);
    expect((await fetch(`${base}/tasks/${task.task_id}/assistant/messages?end=0`)).status).toBe(200);
    await page.reload();
    await page.getByText("HTTP 回归", { exact: true }).click();
    await page.getByText("快照序列化回归", { exact: true }).click();
    await expect(page.getByRole("textbox")).toBeVisible();
    await expect(page.getByText(/后端内部错误/)).toHaveCount(0);
    await page.reload();
    await expect(page.getByRole("textbox")).toBeVisible();
    await expect(page.getByText(/后端内部错误/)).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath("real-snapshot-conversation.png") });
  } finally {
    await app.close();
  }
});


test("A9 real Python HTTP and Electron: final reply derives from stored response; truncation stays open", async () => {
  const { app, page, home } = await launchDesktop();
  try {
    await expect(page.getByRole("navigation", { name: "工作区任务列表" })).toBeVisible({ timeout: 120_000 });
    const base = await backendBaseUrl(page);
    const workspacePath = path.join(home, "timeline-workspace"); fs.mkdirSync(workspacePath);
    const ws = await fetch(`${base}/workspaces`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "时间线真实验收", root_path: workspacePath }) });
    expect(ws.status).toBe(200);
    const { workspace_id } = await ws.json() as { workspace_id: number };
    for (const reason of ["stop", "length"]) {
      const response = await fetch(`${base}/workspaces/${workspace_id}/tasks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: `真实-${reason}`, creationCommandId: `timeline-${reason}` }) });
      expect(response.status).toBe(200);
      const { task_id } = await response.json() as { task_id: number };
      execFileSync(backendPython, [path.join(repositoryRoot, "apps/backend/tests/fixtures/seed_timeline.py"), String(task_id), reason], { cwd: path.join(repositoryRoot, "apps/backend"), env: { ...process.env, CODING_AGENT_DATA_DIR: home } });
      const snapshotResponse = await fetch(`${base}/tasks/${task_id}/assistant/state`);
      expect(snapshotResponse.status).toBe(200);
      const snapshot = await snapshotResponse.json() as { messages: Array<{ parts: Array<{ finalReply?: boolean; text?: string }> }> };
      expect(snapshot.messages.flatMap(m => m.parts).filter(p => p.finalReply).map(p => p.text)).toEqual(reason === "stop" ? ["真实后端结论"] : []);
      await page.reload();
      await page.getByRole("treeitem").filter({ hasText: `真实-${reason}` }).last().click();
      await expect(page.getByText("真实后端结论", { exact: true })).toBeVisible();
      const head = page.locator('[data-slot="process-head"]');
      await expect(head).toHaveAttribute("aria-expanded", String(reason !== "stop"));
      if (reason === "stop") { await expect(page.getByText("真实后端进展", { exact: true })).toBeHidden(); await head.click(); }
      await expect(page.getByText("真实后端进展", { exact: true })).toBeVisible();
      await page.reload();
      await expect(head).toHaveAttribute("aria-expanded", String(reason !== "stop"));
      await expect(page.getByText(/后端内部错误/)).toHaveCount(0);
      await expect(page.locator(".backend-startup-screen")).toHaveCount(0);
      await page.bringToFront();
      await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      await page.screenshot({ path: test.info().outputPath(`real-timeline-${reason}.png`), animations: "disabled" });
    }
  } finally { await app.close(); }
});

test("H7 real Electron: pure completed replies omit headings in both themes and after reload", async () => {
  const { app, page, home } = await launchDesktop();
  try {
    await expect(page.getByRole("navigation", { name: "工作区任务列表" })).toBeVisible({ timeout: 120_000 });
    const base = await backendBaseUrl(page);
    const workspacePath = path.join(home, "heading-workspace");
    fs.mkdirSync(workspacePath);
    const ws = await fetch(`${base}/workspaces`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "标题真实验收", root_path: workspacePath }) });
    expect(ws.status).toBe(200);
    const { workspace_id } = await ws.json() as { workspace_id: number };
    for (const mode of ["plain", "process"]) {
      const response = await fetch(`${base}/workspaces/${workspace_id}/tasks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: `标题-${mode}`, creationCommandId: `heading-${mode}` }) });
      expect(response.status).toBe(200);
      const { task_id } = await response.json() as { task_id: number };
      execFileSync(backendPython, [path.join(repositoryRoot, "apps/backend/tests/fixtures/seed_timeline.py"), String(task_id), "stop", mode], { cwd: path.join(repositoryRoot, "apps/backend"), env: { ...process.env, CODING_AGENT_DATA_DIR: home } });
      const snapshotResponse = await fetch(`${base}/tasks/${task_id}/assistant/state`);
      expect(snapshotResponse.status).toBe(200);
      const snapshot = await snapshotResponse.json() as { messages: Array<{ outcome: { durationMs: number | null } | null; parts: Array<{ finalReply?: boolean; text?: string }> }> };
      expect(snapshot.messages.flatMap(m => m.parts).filter(p => p.finalReply).map(p => p.text)).toEqual(["真实后端结论"]);
      expect(typeof snapshot.messages.at(-1)!.outcome!.durationMs).toBe("number");
      await page.reload();
      await page.getByRole("treeitem").filter({ hasText: `标题-${mode}` }).last().click();
      for (const theme of ["light", "dark"]) {
        await page.evaluate(value => localStorage.setItem("cosir.theme-preference", value), theme);
        await page.reload();
        await expect.poll(() => page.locator("html").evaluate(el => el.classList.contains("dark"))).toBe(theme === "dark");
        const reply = page.getByText("真实后端结论", { exact: true });
        await expect(reply).toBeVisible();
        const heading = page.locator('[data-slot="turn-status"]');
        if (mode === "plain") {
          await expect(heading).toHaveCount(0);
          const prose = page.locator('[data-slot="prose-part"]');
          const turnTop = (await page.locator('[data-slot="turn"]').boundingBox())!.y;
          expect(Math.abs((await prose.boundingBox())!.y - turnTop)).toBeLessThan(1);
          await expect(page.locator('[data-slot="turn"] button').first()).toBeVisible();
        } else {
          const button = heading.getByRole("button");
          await expect(button).toHaveAttribute("aria-expanded", "false");
          const originalReply = await reply.elementHandle();
          await button.focus();
          await page.keyboard.press("Enter");
          await expect(page.getByText("真实后端进展", { exact: true })).toBeVisible();
          await page.keyboard.press("Space");
          await expect(button).toHaveAttribute("aria-expanded", "false");
          await expect(page.getByText("真实后端进展", { exact: true })).toBeHidden();
          expect(await reply.evaluate((el, original) => el === original, originalReply)).toBe(true);
        }
        await expect(page.getByText(/后端内部错误/)).toHaveCount(0);
        await page.bringToFront();
        await page.screenshot({ path: test.info().outputPath(`heading-${mode}-${theme}.png`), animations: "disabled" });
      }
    }
  } finally { await app.close(); }
});

// Only model tokens are controlled: native Electron, HTTP/SSE, Python executor, terminal worker and SQLite are real.
test("I10 real Electron: unified intent travels through real tools, background work and cold reload", async () => {
  const { createServer } = await import("node:http");
  const requests: { body: Record<string, unknown>; response: import("node:http").ServerResponse }[] = [];
  const model = createServer(async (req, res) => {
    let raw = ""; for await (const chunk of req) raw += chunk;
    if (!req.url?.endsWith("/chat/completions")) { res.writeHead(404); res.end(); return; }
    const body = JSON.parse(raw);
    if (!body.stream) {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ id: "native-title", object: "chat.completion", created: 1, model: "native-test", choices: [{ index: 0, message: { role: "assistant", content: JSON.stringify({ title: "意图真实链路" }) }, finish_reason: "stop" }] }));
      return;
    }
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
    res.flushHeaders(); requests.push({ body, response: res });
  });
  await new Promise<void>(resolve => model.listen(0, "127.0.0.1", resolve));
  const address = model.address() as { port: number };
  const { app, page, home } = await launchDesktop();
  const delta = (index: number, value: object, finish: string | null = null) => requests[index].response.write(`data: ${JSON.stringify({ id: `native-${index}`, object: "chat.completion.chunk", created: 1, model: "native-test", choices: [{ index: 0, delta: value, finish_reason: finish }] })}\n\n`);
  const end = (index: number) => requests[index].response.end("data: [DONE]\n\n");
  const tool = (index: number, id: string, name: string, args: object) => {
    delta(index, { tool_calls: [{ index: 0, id, type: "function", function: { name, arguments: JSON.stringify(args) } }] });
    delta(index, {}, "tool_calls"); end(index);
  };
  let releasePath = "";
  try {
    await expect(page.getByRole("navigation", { name: "工作区任务列表" })).toBeVisible({ timeout: 120_000 });
    let base = await backendBaseUrl(page);
    const post = async (route: string, body: object) => {
      const response = await fetch(`${base}${route}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      expect(response.status, await response.clone().text()).toBe(200); return response.json();
    };
    const config = await post("/model-configs", { config_name: "本机意图验收", base_url: `http://127.0.0.1:${address.port}/v1`, api_key: "local-test-only", model_name: "native-test", context_window_k: 128, supports_thinking: true, supports_reasoning_effort: true });
    const root = path.join(home, "intent-workspace"); fs.mkdirSync(root); fs.writeFileSync(path.join(root, "sample.txt"), "真实文件内容");
    releasePath = path.join(root, "release");
    const { workspace_id } = await post("/workspaces", { name: "意图真机验收", root_path: root });
    const { task_id } = await post(`/workspaces/${workspace_id}/tasks`, { text: "意图真实链路", creationCommandId: "native-intent" });
    await page.reload();
    await page.getByRole("treeitem").filter({ hasText: "意图真实链路" }).last().click();
    await post(`/tasks/${task_id}/messages`, { message: { role: "user", parts: [{ type: "text", text: "验证文件与后台终端" }] }, modelConfigId: config.config_id });
    await expect.poll(() => requests.length).toBe(1);
    const schemas = requests[0].body.tools as Array<{ function: { parameters: { required: string[]; properties: Record<string, unknown> } } }>;
    expect(schemas.length).toBeGreaterThan(10);
    expect(schemas.every(item => item.function.parameters.required.filter(name => name === "intent").length === 1)).toBe(true);
    const feedback = page.locator('[data-slot="current-activity"]');
    await expect(feedback).toHaveCount(1);
    delta(0, { reasoning_content: "检查样例" });
    await expect(feedback).toContainText("正在思考");
    tool(0, "native-read", "read_file", { intent: "核对样例文件内容", path: "sample.txt", offset: 1, limit: 500 });
    await expect.poll(() => requests.length).toBe(2);
    const state = async () => (await (await fetch(`${base}/tasks/${task_id}/assistant/state`)).json()) as import("../../lib/assistant/contract").TransportState;
    await expect.poll(async () => (await state()).messages.flatMap(m => m.parts).find(p => p.type === "tool-call" && p.toolCallId === "native-read")).toMatchObject({ intent: "核对样例文件内容", status: "completed" });
    delta(1, { content: "文件核对完成，现在执行终端检查。" });
    // Force text flush before the next tool interval.
    delta(1, { reasoning_content: "准备命令" });
    await expect(page.getByText("文件核对完成，现在执行终端检查。", { exact: true })).toBeVisible();
    tool(1, "native-terminal", "execute_terminal", { intent: "等待验收释放并输出检查结果", command: "while [ ! -f release ]; do sleep 0.1; done; printf 'native-intent-output\\n'", shell: "auto", timeout: 60, workdir: null, background: true });
    await expect.poll(() => requests.length).toBe(3);
    delta(2, { reasoning_content: "后台工作同时进行" });
    await expect(feedback).toHaveCount(1);
    await expect(feedback).toContainText("正在思考 · 后台 1 项");
    await expect(page.locator('[data-slot="turn"] [data-tool-call-id="native-terminal"]')).toHaveCount(0);
    const heads = page.locator('[data-slot="activity-head"]');
    await heads.last().click();
    const terminal = page.locator('[data-slot="turn"] [data-tool-call-id="native-terminal"]');
    await expect(terminal).toContainText("等待验收释放并输出检查结果");
    await expect(terminal).toContainText("后台运行中");
    await expect(terminal).not.toContainText("准备执行");
    await expect(terminal.getByRole("button", { name: "停止终端命令" })).toBeVisible();
    await terminal.locator('[data-slot="call-row"]').click();
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => { document.documentElement.classList.toggle("dark", value === "dark"); }, theme);
      await page.screenshot({ path: test.info().outputPath(`intent-native-${theme}.png`), animations: "disabled" });
    }
    fs.writeFileSync(releasePath, "release");
    await expect.poll(async () => (await state()).background_tasks.find(job => job.toolCallId === "native-terminal")?.status).toBe("settling");
    await expect(terminal).toContainText("后台结果收尾中");
    await expect(terminal.getByRole("button", { name: "停止终端命令" })).toHaveCount(0);
    delta(2, { content: "真实工具验收完成。" }); delta(2, {}, "stop"); end(2);
    // A queued background result may start a follow-up model request; finish it through the same real loop.
    await expect.poll(async () => {
      for (let i = 3; i < requests.length; i++) if (!requests[i].response.writableEnded) { delta(i, { content: "后台输出已核对。" }); delta(i, {}, "stop"); end(i); }
      const snapshot = await state();
      return snapshot.activity?.status === "completed" && snapshot.messages.flatMap(m => m.parts).some(p => p.type === "tool-call" && p.toolCallId === "native-terminal" && p.status === "completed");
    }, { timeout: 30_000 }).toBe(true);
    const before = (await state()).messages.flatMap(m => m.parts).filter(p => p.type === "tool-call");
    expect(before.map(p => p.intent)).toEqual(["核对样例文件内容", "等待验收释放并输出检查结果"]);
    expect(before.every(p => !Object.hasOwn(p.args ?? {}, "intent") && !Object.hasOwn(p.args ?? {}, "description"))).toBe(true);
    const finalReply = (await state()).messages.flatMap(m => m.parts).filter(p => p.type === "text" && p.finalReply).at(-1);
    expect(finalReply?.type).toBe("text");
    await page.evaluate(() => window.cosirHost!.backend.restart());
    base = await backendBaseUrl(page);
    await page.reload();
    await expect(page.getByText(finalReply!.type === "text" ? finalReply!.text : "", { exact: true })).toBeVisible();
    await expect(feedback).toHaveCount(0);
    expect((await state()).messages.flatMap(m => m.parts).filter(p => p.type === "tool-call").map(p => p.intent)).toEqual(before.map(p => p.intent));
    await page.screenshot({ path: test.info().outputPath("intent-native-completed.png"), animations: "disabled" });
  } finally {
    if (releasePath) fs.writeFileSync(releasePath, "release");
    for (const request of requests) request.response.end();
    await app.close();
    model.closeAllConnections(); await new Promise<void>(resolve => model.close(() => resolve()));
  }
});
