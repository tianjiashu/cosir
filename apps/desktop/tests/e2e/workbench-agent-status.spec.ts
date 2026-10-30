import { createServer, type ServerResponse } from "node:http";
import { test as base, expect, type Page } from "@playwright/test";

const origin = "http://127.0.0.1:8000";
const terminal = (status: string) => ["completed", "failed", "cancelled"].includes(status);
/** Task 视角 snapshot：一轮回复 + 当前活动；终态时结局写在回复上。 */
function snapshot(runId: number | null, status = "completed", text = "阅读内容") {
  if (runId === null) return { title: "子 Agent", messages: [], messageOffset: 0, activity: null, terminal_sessions: [], background_tasks: [], foreground_waits: [] };
  const ended = terminal(status);
  return {
    title: "子 Agent",
    messages: [{
      id: `assistant-run-${runId}`, role: "assistant", runId,
      parts: [{ type: "text", text, status: ended ? "completed" : "running" }],
      outcome: ended ? { status, endReason: null, error: null, usage: null, contextWindowTotal: null } : null,
    }],
    messageOffset: 0,
    activity: { runId, status, endReason: null, error: null, usage: null, contextWindowTotal: null,
      launch: { modelConfigId: 2, reasoningEffort: null, disabledToolGroups: [] } },
    terminal_sessions: [], background_tasks: [], foreground_waits: [],
  };
}
type Snapshot = ReturnType<typeof snapshot>;
type Fixture = {
  states: Map<number, Snapshot>;
  snapshots: Map<number, number>;
  /** 真正建立的订阅数：开发模式 StrictMode 挂载后立即中止的那次不算。 */
  attaches: Map<number, number>;
  connections: Map<number, Set<ServerResponse>>;
  statusConnections: Set<ServerResponse>;
  /** 推送新的完整状态；当前活动的状态变了时，同时经状态推送发出（与后端一致）。 */
  push: (id: number, state: Snapshot) => void;
  dropStreams: boolean;
  refuseStatus: boolean;
};
const test = base.extend<{ agent: Fixture }>({
  agent: async ({ page }, use) => {
    const fixture: Fixture = {
      states: new Map([[100, snapshot(null)], [501, snapshot(1, "running")], [502, snapshot(2, "completed")]]),
      snapshots: new Map(), attaches: new Map(), connections: new Map(), statusConnections: new Set(),
      dropStreams: false, refuseStatus: false,
      push(id, state) {
        const before = fixture.states.get(id)?.activity?.status ?? null;
        fixture.states.set(id, state);
        // Task 长订阅：活动到终态也不断开。
        for (const response of fixture.connections.get(id) ?? []) response.write(frame(id, state));
        const after = state.activity?.status ?? null;
        if (after !== before) {
          for (const response of fixture.statusConnections) response.write(`${JSON.stringify({ type: "changed", statuses: { [id]: after } })}\n`);
        }
      },
    };
    function frame(id: number, state: Snapshot) {
      return `data: ${JSON.stringify({ kind: "full", task_id: id, state, mutations: [] })}\n\n`;
    }
    const server = createServer((req, res) => {
      res.setHeader("Access-Control-Allow-Origin", "http://127.0.0.1:4173");
      res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Trace-Id");
      if (req.method === "OPTIONS") { res.writeHead(204); res.end(); return; }
      if (req.url === "/task-status/stream") {
        if (fixture.refuseStatus) { res.writeHead(503); res.end(); return; }
        res.writeHead(200, { "Content-Type": "application/x-ndjson", "Cache-Control": "no-store" });
        const statuses = Object.fromEntries([...fixture.states].map(([id, state]) => [id, state.activity?.status ?? null]));
        res.write(`${JSON.stringify({ type: "full", statuses })}\n`);
        fixture.statusConnections.add(res);
        res.on("close", () => fixture.statusConnections.delete(res));
        return;
      }
      const id = Number(req.url!.split("/")[2]);
      const openedAt = Date.now();
      let endedByServer = false;
      fixture.attaches.set(id, (fixture.attaches.get(id) ?? 0) + 1);
      const connections = fixture.connections.get(id) ?? new Set();
      fixture.connections.set(id, connections);
      connections.add(res);
      res.on("close", () => {
        connections.delete(res);
        // 客户端几乎立刻中止：开发模式 StrictMode 的首次挂载，不算一次订阅。
        if (!endedByServer && Date.now() - openedAt < 300) fixture.attaches.set(id, fixture.attaches.get(id)! - 1);
      });
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
      res.write(frame(id, fixture.states.get(id)!));
      if (fixture.dropStreams) { endedByServer = true; res.end(); }
    });
    await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
    const address = server.address() as { port: number };
    await page.addInitScript(() => { localStorage.clear(); localStorage.setItem("cosir:last-workspace-id", "7"); });
    await page.route(`${origin}/workspaces`, route => route.fulfill({ json: [{ workspace_id: 7, name: "验收工作区", root_path: "/workspace", created_at: "", updated_at: "" }] }));
    const task = (id: number) => ({ task_id: id, workspace_id: 7, title: `任务${id}`, task_type: id === 100 ? "user" : "delegate_task",
      created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" });
    await page.route(`${origin}/workspaces/7/tasks`, route => route.fulfill({ json: [task(100)] }));
    await page.route(/http:\/\/127\.0\.0\.1:8000\/tasks\/\d+$/, route => {
      const id = Number(route.request().url().split("/").at(-1));
      return route.fulfill({ json: task(id) });
    });
    await page.route(`${origin}/task-status/stream`, route => route.continue({ url: `http://127.0.0.1:${address.port}/task-status/stream` }));
    await page.route(`${origin}/tasks/*/assistant/state`, route => {
      const id = Number(route.request().url().split("/")[4]);
      fixture.snapshots.set(id, (fixture.snapshots.get(id) ?? 0) + 1);
      return route.fulfill({ json: fixture.states.get(id) });
    });
    await page.route(`${origin}/tasks/*/assistant/attach`, route => route.continue({ url: route.request().url().replace(origin, `http://127.0.0.1:${address.port}`) }));
    await page.goto("/tasks/100");
    await expect(page.getByRole("button", { name: "浏览工作区文件" })).toBeVisible();
    try { await use(fixture); }
    finally { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); }
  },
});
async function open(page: Page, ids: number[]) {
  await page.evaluate(async taskIds => {
    const path = "/lib/workbench/store.ts";
    const { useWorkbenchStore } = await import(path);
    for (const taskId of taskIds) useWorkbenchStore.getState().openAgent(7, { taskId, title: `Agent${taskId}` });
  }, ids);
}
const tab = (page: Page, id: number) => page.getByRole("tab", { name: new RegExp(`Agent${id}`) });

test("非激活标签跟随状态推送，收起再展开仍是最新，关闭不取消 Run", async ({ page, agent }) => {
  await open(page, [501, 502]);
  await expect(tab(page, 501)).toHaveAccessibleName("运行中 Agent501");
  // 非激活标签只读状态推送，不读快照、不订阅对话。
  expect(agent.snapshots.get(501)).toBeUndefined();
  expect(agent.attaches.get(501)).toBeUndefined();
  agent.push(501, snapshot(1, "failed"));
  await expect(tab(page, 501)).toHaveAccessibleName("运行失败 Agent501");
  await page.getByRole("button", { name: "收起工作台", exact: true }).click();
  agent.push(501, snapshot(3, "running"));
  await page.getByRole("button", { name: /工作台，2 个标签，点击打开/ }).click();
  await expect(tab(page, 501)).toHaveAccessibleName("运行中 Agent501");
  await page.getByRole("button", { name: "关闭 Agent501", exact: true }).click();
  expect(agent.states.get(501)?.activity?.status).toBe("running");
});

test("挂起继续订阅，SSE 状态变化刷新标签，后续新 Run 经同一订阅接续", async ({ page, agent }, testInfo) => {
  agent.push(501, snapshot(1, "suspended", "等待确认内容"));
  await open(page, [501]);
  await expect.poll(() => agent.attaches.get(501)).toBe(1);
  await expect(tab(page, 501)).toHaveAccessibleName("等待中 Agent501");
  await tab(page, 501).focus();
  await page.screenshot({ path: testInfo.outputPath("agent-waiting.png") });
  const count = agent.snapshots.get(501);
  agent.push(501, snapshot(1, "running", "正在继续执行"));
  await expect(tab(page, 501)).toHaveAccessibleName("运行中 Agent501");
  await expect(page.getByText("正在继续执行", { exact: true })).toBeVisible();
  expect(agent.snapshots.get(501)).toBe(count);
  agent.push(501, snapshot(1, "completed", "第一轮完成"));
  await expect(tab(page, 501)).toHaveAccessibleName("已完成 Agent501");
  await expect(page.getByText("第一轮完成", { exact: true })).toBeVisible();
  expect(agent.snapshots.get(501)).toBe(count);
  agent.push(501, snapshot(4, "running", "新轮次内容"));
  await expect(page.getByText("新轮次内容", { exact: true })).toBeVisible();
  expect(agent.attaches.get(501)).toBe(1);
});

test("已停止的子 Task 也常驻订阅，恢复经同一订阅到达", async ({ page, agent }) => {
  agent.push(501, snapshot(1, "cancelled", "已停止的内容"));
  await open(page, [501]);
  await expect(tab(page, 501)).toHaveAccessibleName("已停止 Agent501");
  await expect(page.getByText("已停止的内容", { exact: true })).toBeVisible();
  await expect.poll(() => agent.attaches.get(501)).toBe(1);
  agent.push(501, snapshot(1, "running", "恢复后的内容"));
  await expect(page.getByText("恢复后的内容", { exact: true })).toBeVisible();
  expect(agent.attaches.get(501)).toBe(1);
});

test("状态推送断开时标签保留上次状态并标为未同步，重连后恢复", async ({ page, agent }) => {
  await open(page, [501, 502]);
  await expect(tab(page, 501)).toHaveAccessibleName("运行中 Agent501");
  agent.refuseStatus = true;
  for (const response of agent.statusConnections) response.end();
  await expect(tab(page, 501)).toHaveAccessibleName("状态未同步，上次为运行中 Agent501");
  agent.states.set(501, snapshot(1, "completed"));
  agent.refuseStatus = false;
  // 重连后的整表帧带回断开期间的变化。
  await expect(tab(page, 501)).toHaveAccessibleName("已完成 Agent501", { timeout: 10_000 });
});

test("意外 EOF 与主对话同一套有限重连，只重开订阅不重读 snapshot，手动重试恢复完整预算", async ({ page, agent }) => {
  agent.dropStreams = true;
  await open(page, [501]);
  // 主 Task 页同样常驻订阅并会因断流给出自己的提示；这里只看工作台阅读页。
  const workbench = page.getByLabel("工作台", { exact: true });
  const exhausted = workbench.getByText("本机后端连接多次中断，请重新同步恢复对话。", { exact: true });
  await expect(workbench.getByText("阅读内容", { exact: true })).toBeVisible();
  // 首屏读取次数（开发模式 StrictMode 会让首屏 effect 多跑一次）；之后的重连不再读 snapshot。
  const initialReads = agent.snapshots.get(501);
  await expect(exhausted).toBeVisible();
  // 首次订阅 + 5 次重连后停止；断流期间已显示的内容保留。
  expect(agent.attaches.get(501)).toBe(6);
  expect(agent.snapshots.get(501)).toBe(initialReads);
  await expect(workbench.getByText("阅读内容", { exact: true })).toBeVisible();
  await page.waitForTimeout(3_300);
  expect(agent.attaches.get(501)).toBe(6);
  await workbench.getByRole("button", { name: "重新同步", exact: true }).click();
  await expect.poll(() => agent.attaches.get(501)).toBe(12);
  await expect(exhausted).toBeVisible();
  expect(agent.snapshots.get(501)).toBe(initialReads);
});
