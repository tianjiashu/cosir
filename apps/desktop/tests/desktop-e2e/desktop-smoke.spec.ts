import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";

import { backendBaseUrl, backendPython, launchDesktop, repositoryRoot } from "./desktop-app";

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
      const snapshot = await snapshotResponse.json() as { messages: Array<{ outcome: { durationMs: number } | null; parts: Array<{ finalReply?: boolean; text?: string }> }> };
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
    const config = (await post("/configuration/model-connections", { name: "本机意图验收", base_url: `http://127.0.0.1:${address.port}/v1`, api_key: "local-test-only", models: [{ model_name: "native-test", context_window_k: 128, supports_thinking: true, supports_reasoning_effort: true }] }) as { models: { config_id: string; connection_id: string; model_name: string }[] }).models[0]!;
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
    // 后台命令在时间线只留一行，停止在看板上。
    const stopOnBoard = page.locator('[data-background-task="native-terminal"]').getByRole("button", { name: "停止", exact: true });
    await expect(stopOnBoard).toBeVisible();
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => { document.documentElement.classList.toggle("dark", value === "dark"); }, theme);
      await page.screenshot({ path: test.info().outputPath(`intent-native-${theme}.png`), animations: "disabled" });
    }
    fs.writeFileSync(releasePath, "release");
    await expect.poll(async () => (await state()).background_tasks.find(job => job.toolCallId === "native-terminal")?.status).toBe("settling");
    await expect(terminal).toContainText("后台结果收尾中");
    await expect(stopOnBoard).toHaveCount(0);
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

// Genuine Electron + Python + SQLite + SDK requests; only the model's output is deterministic.
test("fork context: reply boundary, presentation, restart and source deletion", async () => {
  const { createServer } = await import("node:http");
  type RequestBody = { model: string; reasoning_effort?: string; tools?: unknown[]; messages: Array<{ role: string; content?: unknown; tool_calls?: unknown[] }>; stream: boolean };
  const requests: RequestBody[] = [];
  const model = createServer(async (req, res) => {
    let raw = ""; for await (const chunk of req) raw += chunk;
    if (!req.url?.endsWith("/chat/completions")) { res.writeHead(404).end(); return; }
    const body = JSON.parse(raw) as RequestBody;
    if (!body.stream) {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ id: "title", object: "chat.completion", created: 1, model: "fork-test", choices: [{ index: 0, message: { role: "assistant", content: '{"title":"分叉验收源"}' }, finish_reason: "stop" }] })); return;
    }
    requests.push(body);
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const emit = (delta: object, finish_reason: string | null = null) => res.write(`data: ${JSON.stringify({ id: "fork-test", object: "chat.completion.chunk", created: 1, model: "fork-test", choices: [{ index: 0, delta, finish_reason }] })}\n\n`);
    const inputs = body.messages.filter(m => m.role === "user").map(m => JSON.stringify(m.content));
    const isChild = inputs.some(text => text.includes("CHILD_SCOPE_C"));
    const parentTrigger = inputs.some(text => text.includes("PARENT_ONLY_B"));
    const hasFork = body.messages.some(m => JSON.stringify(m.tool_calls ?? []).includes("native-fork"));
    if (parentTrigger && !isChild && !hasFork) {
      emit({ tool_calls: [{ index: 0, id: "native-fork", type: "function", function: { name: "fork_task", arguments: JSON.stringify({ intent: "验证从上一条回复分叉", title: "分叉验收子任务", message: "CHILD_SCOPE_C：只核对继承的背景，回复 CHILD_DONE_D。" }) } }] });
      emit({}, "tool_calls");
    } else { emit({ content: isChild ? "CHILD_DONE_D" : parentTrigger ? "PARENT_DONE" : "KEEP_REPLY_A" }); emit({}, "stop"); }
    res.end("data: [DONE]\n\n");
  });
  await new Promise<void>(resolve => model.listen(0, "127.0.0.1", resolve));
  const address = model.address() as { port: number };
  const { app, page, home } = await launchDesktop();
  try {
    await expect(page.getByRole("navigation", { name: "工作区任务列表" })).toBeVisible({ timeout: 120_000 });
    let base = await backendBaseUrl(page);
    const post = async (route: string, body: object) => {
      const response = await fetch(`${base}${route}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      expect(response.status, await response.clone().text()).toBe(200); return response.json();
    };
    const state = async (id: number) => (await (await fetch(`${base}/tasks/${id}/assistant/state`)).json()) as import("../../lib/assistant/contract").TransportState;
    const replacement = (await post("/configuration/model-connections", { name: "目录默认模型", base_url: `http://127.0.0.1:${address.port}/v1`, api_key: "local-test-only", models: [{ model_name: "decoy-test", context_window_k: 128, supports_thinking: true, supports_reasoning_effort: true }] }) as { models: { config_id: string; connection_id: string; model_name: string }[] }).models[0]!;
    const config = (await post("/configuration/model-connections", { name: "本机分叉验收", base_url: `http://127.0.0.1:${address.port}/v1`, api_key: "local-test-only", models: [{ model_name: "fork-test", context_window_k: 128, supports_thinking: true, supports_reasoning_effort: true }] }) as { models: { config_id: string; connection_id: string; model_name: string }[] }).models[0]!;
    const root = path.join(home, "fork-workspace"); fs.mkdirSync(root);
    const { workspace_id } = await post("/workspaces", { name: "分叉真机验收", root_path: root });
    const { task_id } = await post(`/workspaces/${workspace_id}/tasks`, { text: "分叉验收源", creationCommandId: "native-fork-source" });
    const send = (id: number, text: string) => post(`/tasks/${id}/messages`, { message: { role: "user", parts: [{ type: "text", text }] }, modelConfigId: config.config_id, reasoningEffort: "low" });
    await send(task_id, "KEEP_CONTEXT_A");
    await expect.poll(async () => (await state(task_id)).activity?.status).toBe("completed");
    await send(task_id, "PARENT_ONLY_B：测试一下 fork task tool");
    const list = async () => (await (await fetch(`${base}/workspaces/${workspace_id}/tasks`)).json()) as Array<{task_id: number; task_type: string; title: string}>;
    await expect.poll(async () => (await list()).filter(t => ["user_fork", "agent_fork"].includes(t.task_type)).length).toBe(1);
    const child = (await list()).find(t => ["user_fork", "agent_fork"].includes(t.task_type))!;
    await expect.poll(async () => (await state(child.task_id)).activity?.status).toBe("completed");
    await expect.poll(async () => (await state(task_id)).activity?.status).toBe("completed");
    const childRequest = requests.find(body => body.messages.some(m => m.role === "user" && JSON.stringify(m.content).includes("CHILD_SCOPE_C")))!;
    expect(childRequest).toBeTruthy();
    expect(JSON.stringify(childRequest.messages)).toContain("KEEP_CONTEXT_A");
    expect(JSON.stringify(childRequest.messages)).toContain("KEEP_REPLY_A");
    expect(JSON.stringify(childRequest.messages)).not.toContain("PARENT_ONLY_B");
    expect(JSON.stringify(childRequest.messages)).not.toContain("native-fork");
    // 工具列表与来源相同才接得上缓存；交接通知紧贴任务说明，并告知 fork_task 不可用。
    expect(childRequest.tools).toEqual(requests.find(body => JSON.stringify(body.messages).includes("PARENT_ONLY_B"))!.tools);
    const [handover, assignment] = childRequest.messages.slice(-2).map(m => JSON.stringify(m.content));
    expect(handover).toContain(`You are task ${child.task_id}, a fork of task ${task_id}`);
    expect(handover).toMatch(/disabled for this round[^.]*fork_task/);
    expect(assignment).toContain("CHILD_SCOPE_C");
    const childState = await state(child.task_id);
    expect(childState.fork?.taskType).toBe("agent_fork");
    expect(JSON.stringify(childState.messages)).not.toContain("KEEP_CONTEXT_A");
    expect(childState.messages[0].source).not.toBeNull();
    await page.reload();
    await page.getByRole("treeitem").filter({ hasText: "分叉验收子任务" }).last().click();
    await expect(page.locator('[data-slot="fork-origin"]')).toContainText("历史仅供上下文使用");
    await expect(page.getByText("CHILD_DONE_D", { exact: true })).toBeVisible();
    await expect(page.locator('[data-role="user"]')).toHaveCount(0);
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => document.documentElement.classList.toggle("dark", value === "dark"), theme);
      await page.screenshot({ path: test.info().outputPath(`fork-agent-${theme}.png`), animations: "disabled" });
    }
    // Real click on the latest reply: the branch keeps it, and hidden inherited history comes back.
    await page.getByRole("button", { name: "从这里分叉", exact: true }).last().click();
    await expect.poll(async () => (await list()).filter(t => ["user_fork", "agent_fork"].includes(t.task_type)).length).toBe(2);
    const manual = (await list()).find(t => ["user_fork", "agent_fork"].includes(t.task_type) && t.task_id !== child.task_id)!;
    await expect(page.getByText("KEEP_CONTEXT_A", { exact: true })).toBeVisible();
    await expect(page.getByText("KEEP_REPLY_A", { exact: true })).toBeVisible();
    expect((await state(manual.task_id)).activity).toBeNull();
    expect(JSON.stringify((await state(manual.task_id)).messages)).toContain("CHILD_SCOPE_C");
    await expect(page.getByText("CHILD_DONE_D", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "撤回" })).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath("fork-manual.png"), animations: "disabled" });
    // An earlier reply stops at that reply; the latest one keeps the whole conversation.
    const sourceState = await state(task_id);
    const earlierReply = sourceState.messages.find(m => m.role === "assistant" && JSON.stringify(m.parts).includes("KEEP_REPLY_A"))!;
    const human = await post(`/tasks/${task_id}/fork`, { replyMessageId: earlierReply.id });
    expect(JSON.stringify((await state(human.task_id)).messages)).toContain("KEEP_REPLY_A");
    expect(JSON.stringify((await state(human.task_id)).messages)).not.toContain("PARENT_ONLY_B");
    const latest = await post(`/tasks/${task_id}/fork`, { replyMessageId: sourceState.messages.at(-1)!.id });
    expect(JSON.stringify((await state(latest.task_id)).messages)).toContain("PARENT_DONE");
    // Restart with generation verification before switching the dynamic HTTP address.
    const generation = await page.evaluate(async () => (await window.cosirHost!.backend.snapshot()).generation);
    await page.evaluate(() => window.cosirHost!.backend.restart());
    await expect.poll(async () => { const s = await page.evaluate(() => window.cosirHost!.backend.snapshot()); return s.generation > generation && s.status.state === "ready"; }, { timeout: 60_000 }).toBe(true);
    base = await backendBaseUrl(page);
    await page.reload();
    expect((await state(manual.task_id)).activity).toBeNull();
    expect(JSON.stringify((await state(child.task_id)).messages)).not.toContain("KEEP_CONTEXT_A");
    const deleted = await fetch(`${base}/tasks/${task_id}`, { method: "DELETE" });
    expect(deleted.ok, await deleted.text()).toBe(true);
    const before = requests.length;
    // Send through the real composer, proving its initial model/effort comes from the fork,
    // even when a different model is first in the catalogue.
    await page.getByRole("textbox", { name: "消息输入", exact: true }).fill("AFTER_SOURCE_DELETE");
    await page.getByRole("textbox", { name: "消息输入", exact: true }).press("Enter");
    await expect.poll(() => requests.length).toBeGreaterThan(before);
    const after = requests.find(r => r.messages.some(m => m.role === "user" && JSON.stringify(m.content).includes("AFTER_SOURCE_DELETE")))!;
    expect(after.model).toBe("fork-test");
    expect(after.reasoning_effort).toBe("low");
    expect(JSON.stringify(after.messages)).toContain("KEEP_CONTEXT_A");
    expect(JSON.stringify(after.messages)).not.toContain("PARENT_ONLY_B");
    await expect.poll(async () => (await state(manual.task_id)).activity?.status).toBe("completed");
    // A frozen seed is provenance. The existing admission boundary rejects a deleted model,
    // while the human can choose a replacement for a branch that still has no local Run.
    const removedModel = await fetch(`${base}/configuration/model-connections/${config.connection_id}/models/${encodeURIComponent(config.model_name)}`, { method: "DELETE" });
    expect(removedModel.ok, await removedModel.text()).toBe(true);
    const requestCount = requests.length;
    const rejected = await fetch(`${base}/tasks/${human.task_id}/messages`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: { role: "user", parts: [{ type: "text", text: "REJECT_DELETED_MODEL" }] }, modelConfigId: config.config_id }),
    });
    expect(rejected.status).toBe(400);
    expect(await rejected.text()).toContain("MODEL_CONFIG_UNAVAILABLE");
    expect((await state(human.task_id)).activity).toBeNull();
    expect((await state(human.task_id)).queued_messages ?? []).toEqual([]);
    expect(requestCount).toBe(requests.length);
    await post(`/tasks/${human.task_id}/messages`, {
      message: { role: "user", parts: [{ type: "text", text: "REPLACEMENT_MODEL_RUN" }] }, modelConfigId: replacement.config_id,
    });
    await expect.poll(async () => (await state(human.task_id)).activity?.status).toBe("completed");
    expect(requests.find(r => r.messages.some(m => JSON.stringify(m.content).includes("REPLACEMENT_MODEL_RUN")))?.model).toBe("decoy-test");
    await test.info().attach("actual-model-requests", { body: JSON.stringify(requests, null, 2), contentType: "application/json" });
    await test.info().attach("isolated-data-root", { body: home, contentType: "text/plain" });
  } finally { await app.close(); model.closeAllConnections(); await new Promise<void>(resolve => model.close(() => resolve())); }
});
