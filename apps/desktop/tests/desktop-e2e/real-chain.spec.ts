import fs from "node:fs";
import { createServer, type Server } from "node:http";
import path from "node:path";
import { expect, test, type ElectronApplication, type Page } from "@playwright/test";

import type { TransportState } from "../../lib/assistant/contract";
import { backendBaseUrl, launchDesktop } from "./desktop-app";
import { attachFirstFrame } from "./attach-first-frame";

/**
 * 只替换模型输出，Electron、HTTP/SSE、Python、SQLite 与 OpenAI SDK 都是真实的：这几条路径的缺陷
 * 只在真实厂商响应与真实持久化相遇时出现，内存测试服务复现不了。
 */
type ToolCall = { id: string; name: string; args: object };
type Turn = { text?: string; calls?: ToolCall[]; dropMidCall?: boolean; refuse?: string };
type ChatMessage = { role: string; content: unknown; tool_call_id?: string; tool_calls?: { id: string }[] };

/** 按 `turns` 的顺序应答流式请求。 */
async function startModel() {
  const turns: Turn[] = [];
  const requests: { messages: ChatMessage[] }[] = [];
  const server = createServer(async (req, res) => {
    let raw = ""; for await (const part of req) raw += part;
    if (!req.url?.endsWith("/chat/completions")) { res.writeHead(404); res.end(); return; }
    const body = JSON.parse(raw);
    // 非流式请求是标题支线；让它失败，侧栏标题保持创建时的值，用例才能按标题切换 Task。
    if (!body.stream) { res.writeHead(400); res.end(); return; }
    requests.push(body);
    const turn = turns.shift() ?? { text: "脚本之外的请求" };
    if (turn.refuse) {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: { message: turn.refuse } }));
      return;
    }
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
    const chunk = (delta: object, finish: string | null = null) => res.write(`data: ${JSON.stringify({ id: "scripted", object: "chat.completion.chunk", created: 1, model: body.model, choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`);
    if (turn.dropMidCall) {
      chunk({ tool_calls: [{ index: 0, id: "cut-off", type: "function", function: { name: "read_file", arguments: '{"intent":"读到一半' } }] });
      setTimeout(() => res.destroy(), 100);
      return;
    }
    if (turn.calls) {
      chunk({ tool_calls: turn.calls.map((call, index) => ({ index, id: call.id, type: "function", function: { name: call.name, arguments: JSON.stringify(call.args) } })) });
      chunk({}, "tool_calls");
    } else {
      chunk({ content: turn.text });
      chunk({}, "stop");
    }
    res.end("data: [DONE]\n\n");
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  return { server, turns, requests, port: (server.address() as { port: number }).port };
}

/** 一个工作区、若干 Task、一份模型配置。`base` 随后端重启变化，只经 `env` 读取。 */
class Env {
  base = "";
  root = "";
  modelConfigId = "";
  private constructor(readonly app: ElectronApplication, readonly page: Page, readonly home: string, readonly model: Awaited<ReturnType<typeof startModel>>) {}

  static async open(titles: string[]): Promise<{ env: Env; tasks: number[] }> {
    const model = await startModel();
    const { app, page, home } = await launchDesktop();
    const env = new Env(app, page, home, model);
    await expect(page.getByRole("navigation", { name: "工作区任务列表" })).toBeVisible({ timeout: 120_000 });
    env.base = await backendBaseUrl(page);
    const config = (await env.post("/configuration/model-connections", { name: "真实链路", base_url: `http://127.0.0.1:${model.port}/v1`, api_key: "local-test-only", models: [{ model_name: "chain-test", context_window_k: 128, supports_thinking: true, supports_reasoning_effort: true }] }) as { models: { config_id: string; connection_id: string; model_name: string }[] }).models[0]!;
    env.modelConfigId = config.config_id;
    env.root = path.join(home, "chain-workspace"); fs.mkdirSync(env.root);
    const { workspace_id } = await env.post("/workspaces", { name: "真实链路验收", root_path: env.root }) as { workspace_id: number };
    const tasks: number[] = [];
    for (const title of titles) tasks.push((await env.post(`/workspaces/${workspace_id}/tasks`, { text: title, creationCommandId: `chain-${title}` }) as { task_id: number }).task_id);
    await page.reload();
    return { env, tasks };
  }

  async post(route: string, body: object): Promise<unknown> {
    const response = await fetch(`${this.base}${route}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    expect(response.status, await response.clone().text()).toBe(200);
    return response.json();
  }

  send(task: number, text: string): Promise<unknown> {
    return this.post(`/tasks/${task}/messages`, { message: { role: "user", parts: [{ type: "text", text }] }, modelConfigId: this.modelConfigId });
  }

  async state(task: number): Promise<TransportState> {
    return await attachFirstFrame(this.base, task);
  }

  /** 命令返回时新 Run 可能还没出现，所以等这一轮的回复，而不是等状态落到终态。 */
  async replied(task: number, text: string): Promise<void> {
    await expect.poll(async () => {
      const state = await this.state(task);
      return state.activity?.status === "completed" && state.messages.flatMap(message => message.parts).some(part => part.type === "text" && part.text === text);
    }, { timeout: 30_000 }).toBe(true);
  }

  async open(title: string): Promise<void> {
    await this.page.getByRole("treeitem").filter({ hasText: title }).last().click();
  }

  /** 冷重建只在后端换代后发生：内存里的工作副本随旧进程消失。 */
  async restartBackend(): Promise<void> {
    const generation = await this.page.evaluate(async () => (await window.cosirHost!.backend.snapshot()).generation);
    await this.page.evaluate(() => window.cosirHost!.backend.restart());
    await expect.poll(async () => {
      const snapshot = await this.page.evaluate(() => window.cosirHost!.backend.snapshot());
      return snapshot.generation > generation && snapshot.status.state === "ready";
    }, { timeout: 60_000 }).toBe(true);
    this.base = await backendBaseUrl(this.page);
    await this.page.reload();
  }

  async close(): Promise<void> {
    await this.app.close();
    this.model.server.closeAllConnections();
    await new Promise<void>(resolve => (this.model.server as Server).close(() => resolve()));
  }
}

const toolParts = (state: TransportState) => state.messages.flatMap(message => message.parts).flatMap(part => part.type === "tool-call" ? [part] : []);
const toolSummary = (state: TransportState) => toolParts(state).map(part => ({ id: part.toolCallId, status: part.status, intent: part.intent }));

/** 模型协议的底线：每个发出去的调用都有且只有一条结果，结果不串到别的调用上。 */
function resultsById(messages: ChatMessage[]): Map<string, string> {
  const results = new Map<string, string>();
  for (const message of messages) if (message.role === "tool") {
    expect(results.has(message.tool_call_id!), `重复的结果 ${message.tool_call_id}`).toBe(false);
    results.set(message.tool_call_id!, JSON.stringify(message.content));
  }
  const calls = messages.flatMap(message => message.tool_calls ?? []).map(call => call.id);
  expect(new Set(calls).size).toBe(calls.length);
  expect([...results.keys()].sort()).toEqual([...calls].sort());
  return results;
}

let env: Env | undefined;
test.afterEach(async () => { await env?.close(); env = undefined; });

// 目的：厂商在一次响应内、跨响应、跨 Run 复用同一个 id 时，每个调用的结果、卡片与重建都各归各的；
// 潜在缺陷：按 id 寻址的结果互相覆盖，或冷重建把几次调用合并成一张卡片。
test("a vendor reusing one tool call id keeps every call apart, live and after a restart", async () => {
  const opened = await Env.open(["重复调用"]);
  env = opened.env;
  const [task] = opened.tasks;
  for (const name of ["a", "b", "c", "d"]) fs.writeFileSync(path.join(env.root, `${name}.txt`), `内容-${name}`);
  const read = (name: string): ToolCall => ({ id: "dup", name: "read_file", args: { intent: `读取 ${name}`, path: `${name}.txt`, offset: 1, limit: 500 } });
  await env.open("重复调用");
  env.model.turns.push({ calls: [read("a"), read("b")] }, { calls: [read("c")] }, { text: "第一轮完成" });
  await env.send(task, "第一轮");
  await env.replied(task, "第一轮完成");
  env.model.turns.push({ calls: [read("d")] }, { text: "第二轮完成" });
  await env.send(task, "第二轮");
  await env.replied(task, "第二轮完成");

  // 后缀规则归后端；这里只要求第一个保持厂商原值、其余互不相同，且结果各归各的。
  const expected = toolSummary(await env.state(task));
  expect(expected.map(part => [part.status, part.intent])).toEqual(["a", "b", "c", "d"].map(name => ["completed", `读取 ${name}`]));
  expect(expected[0].id).toBe("dup");
  expect(new Set(expected.map(part => part.id)).size).toBe(4);
  const results = resultsById(env.model.requests.at(-1)!.messages);
  expected.forEach(({ id }, index) => expect(results.get(id)).toContain(`内容-${"abcd"[index]}`));

  await env.restartBackend();
  expect(toolSummary(await env.state(task))).toEqual(expected);
  await expect(env.page.getByText("第二轮完成", { exact: true })).toBeVisible();
  for (const slot of ["process-head", "activity-head"]) {
    const closed = env.page.locator(`[data-slot="${slot}"][aria-expanded="false"]`);
    while (await closed.count()) await closed.first().click();
  }
  for (const { id } of expected) await expect(env.page.locator(`[data-slot="turn"] [data-tool-call-id="${id}"]`)).toHaveCount(1);
});

async function withdrawLatest(page: Page) {
  const user = page.locator('[data-role="user"]').last();
  await user.hover();
  await user.getByRole("button", { name: "撤回" }).click();
}

// 目的：撤回走真实的移出，内容回到输入框、改完照常发送；目标在途时变了由后端拒绝，输入框不动；
// 潜在缺陷：撤回后模型仍看到旧消息，或 409 之后内容被放回输入框。
test("a withdrawn message goes back to the composer, the model no longer sees it, and a stale withdraw is refused", async () => {
  const opened = await Env.open(["撤回甲"]);
  env = opened.env;
  const [task] = opened.tasks;
  const { page } = env;
  await env.open("撤回甲");
  env.model.turns.push({ text: "第一次回答" });
  await env.send(task, "原始问题");
  await env.replied(task, "第一次回答");

  await withdrawLatest(page);
  const composer = page.getByLabel("消息输入");
  await expect(composer).toContainText("原始问题");
  await expect(page.locator('[data-role="user"]')).toHaveCount(0);
  await composer.click();
  await page.keyboard.press("End");
  await page.keyboard.type("，加上改动");
  env.model.turns.push({ text: "改过之后的回答" });
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await env.replied(task, "改过之后的回答");
  const resent = JSON.stringify(env.model.requests.at(-1)!.messages);
  expect(resent).toContain("原始问题，加上改动");
  expect(resent).not.toContain('"原始问题"');
  expect(resent).not.toContain("第一次回答");

  // 撤回请求在途时目标才变：只有后端能发现，以 WITHDRAW_TARGET_STALE 拒绝。放行前让新消息先落定。
  const withdrawRoute = `${env.base}/tasks/${task}/messages/*/withdraw`;
  await page.route(withdrawRoute, async (route) => {
    env!.model.turns.push({ text: "第二条回答" });
    await env!.send(task, "在途时发出的新消息");
    await env!.replied(task, "第二条回答");
    await route.continue();
  });
  await withdrawLatest(page);
  await expect(page.getByText("撤回失败")).toBeVisible();
  await page.unroute(withdrawRoute);
  await expect(composer).not.toContainText("加上改动");
  await expect(page.getByText("在途时发出的新消息", { exact: true })).toBeVisible();
});

// 目的：模型调用失败时 Run 落定、卡片不悬空、后续模型请求的协议完整；
// 潜在缺陷：断流留下的半截调用永远显示进行中，或下一次请求带着没有结果的调用被厂商拒绝。
test("a model failure settles the run and leaves the task usable, live and after a restart", async () => {
  const opened = await Env.open(["失败路径"]);
  env = opened.env;
  const [task] = opened.tasks;
  const { page } = env;
  fs.writeFileSync(path.join(env.root, "a.txt"), "内容-a");
  await env.open("失败路径");

  env.model.turns.push({ calls: [{ id: "kept", name: "read_file", args: { intent: "读取 a", path: "a.txt", offset: 1, limit: 500 } }] }, { dropMidCall: true });
  await env.send(task, "第一次");
  await expect.poll(async () => (await env!.state(task)).activity?.status, { timeout: 30_000 }).toBe("failed");
  const settled = toolSummary(await env.state(task));
  expect(settled.find(part => part.id === "kept")?.status).toBe("completed");
  expect(settled.filter(part => part.status === "pending" || part.status === "running")).toEqual([]);

  env.model.turns.push({ refuse: "测试模型拒绝了请求" });
  await env.send(task, "第二次");
  await expect(page.getByText("测试模型拒绝了请求").first()).toBeVisible({ timeout: 30_000 });
  await expect.poll(async () => (await env!.state(task)).activity?.status).toBe("failed");

  env.model.turns.push({ text: "恢复后的回答" });
  await env.send(task, "第三次");
  await env.replied(task, "恢复后的回答");
  const results = resultsById(env.model.requests.at(-1)!.messages);
  expect(results.get("kept")).toContain("内容-a");

  const live = toolSummary(await env.state(task));
  await env.restartBackend();
  expect(toolSummary(await env.state(task))).toEqual(live);
  await expect(page.getByText("恢复后的回答", { exact: true })).toBeVisible();
});
