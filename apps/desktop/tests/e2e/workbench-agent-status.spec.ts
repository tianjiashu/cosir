import { createServer, type ServerResponse } from "node:http";
import { test as base, expect, type Page } from "@playwright/test";

const origin = "http://127.0.0.1:8000";
const terminal = (status: string) => ["completed", "failed", "cancelled"].includes(status);
function snapshot(runId: number | null, status = "completed", text = "阅读内容") {
  return { current_run_id: runId, approvals: {}, context_window_total: null, error: null,
    runs: runId === null ? [] : [{ runId, status, endReason: null, langfuseTraceId: null, usage: null, error: null,
      messages: [{ id: `assistant-${runId}`, role: "assistant", parts: [{ type: "text", text, status: terminal(status) ? "completed" : "running" }] }] }],
  };
}
type Snapshot = ReturnType<typeof snapshot>;
type Fixture = {
  states: Map<number, Snapshot>;
  errors: Map<number, number>;
  reads: Map<number, number>;
  snapshots: Map<number, number>;
  attaches: Map<number, number>;
  connections: Map<number, Set<ServerResponse>>;
  push: (id: number, state: Snapshot) => void;
  dropStreams: boolean;
};
const test = base.extend<{ agent: Fixture }>({
  agent: async ({ page }, use) => {
    const fixture: Fixture = {
      states: new Map([[100, snapshot(null)], [501, snapshot(1, "running")], [502, snapshot(2, "completed")]]),
      errors: new Map(), reads: new Map(), snapshots: new Map(), attaches: new Map(), connections: new Map(),
      dropStreams: false,
      push(id, state) {
        fixture.states.set(id, state);
        for (const response of fixture.connections.get(id) ?? []) {
          response.write(frame(id, state));
          if (terminal(state.runs[0].status)) response.end();
        }
      },
    };
    function frame(id: number, state: Snapshot) {
      return `data: ${JSON.stringify({ kind: "full", task_id: id, state, mutations: [], target_run_id: state.current_run_id, target_run_status: state.runs[0]?.status ?? null })}\n\n`;
    }
    const server = createServer((req, res) => {
      res.setHeader("Access-Control-Allow-Origin", "http://127.0.0.1:4173");
      res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Trace-Id");
      if (req.method === "OPTIONS") { res.writeHead(204); res.end(); return; }
      const id = Number(req.url!.split("/")[2]);
      fixture.attaches.set(id, (fixture.attaches.get(id) ?? 0) + 1);
      const connections = fixture.connections.get(id) ?? new Set();
      fixture.connections.set(id, connections);
      connections.add(res);
      res.on("close", () => connections.delete(res));
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
      res.write(frame(id, fixture.states.get(id)!));
      if (fixture.dropStreams) res.end();
    });
    await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
    const address = server.address() as { port: number };
    await page.addInitScript(() => { localStorage.clear(); localStorage.setItem("cosir:last-workspace-id", "7"); });
    await page.route(`${origin}/workspaces`, route => route.fulfill({ json: [{ workspace_id: 7, name: "验收工作区", root_path: "/workspace", created_at: "", updated_at: "" }] }));
    const task = (id: number) => ({ task_id: id, workspace_id: 7, title: `任务${id}`, task_type: id === 100 ? "user" : "delegate_task",
      current_run_id: fixture.states.get(id)?.current_run_id ?? null, execution_status: fixture.states.get(id)?.runs[0]?.status ?? null,
      context_window_total: null, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" });
    await page.route(`${origin}/workspaces/7/tasks`, route => route.fulfill({ json: [task(100)] }));
    await page.route(/http:\/\/127\.0\.0\.1:8000\/tasks\/\d+$/, route => {
      const id = Number(route.request().url().split("/").at(-1));
      fixture.reads.set(id, (fixture.reads.get(id) ?? 0) + 1);
      const status = fixture.errors.get(id);
      return route.fulfill(status ? { status, json: { detail: "读取失败" } } : { json: task(id) });
    });
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
    for (const taskId of taskIds) useWorkbenchStore.getState().openAgentTab({ workspaceId: 7, taskId, title: `Agent${taskId}`, role: "审查" });
  }, ids);
}
const tab = (page: Page, id: number) => page.getByRole("tab", { name: new RegExp(`Agent${id}`) });

test("非激活标签刷新、收起暂停、展开更新、关闭不取消 Run", async ({ page, agent }) => {
  await open(page, [501, 502]);
  await expect(tab(page, 501)).toHaveAccessibleName("运行中 Agent501");
  expect(agent.snapshots.get(501)).toBeUndefined();
  expect(agent.attaches.get(501)).toBeUndefined();
  agent.states.set(501, snapshot(1, "failed"));
  await expect(tab(page, 501)).toHaveAccessibleName("运行失败 Agent501", { timeout: 6_000 });
  await page.getByRole("button", { name: "收起工作台", exact: true }).click();
  const count = agent.reads.get(501);
  await page.waitForTimeout(3_300);
  expect(agent.reads.get(501)).toBe(count);
  agent.states.set(501, snapshot(3, "running"));
  await page.getByRole("button", { name: /工作台，2 个标签，点击打开/ }).click();
  await expect(tab(page, 501)).toHaveAccessibleName("运行中 Agent501");
  await page.getByRole("button", { name: "关闭 Agent501", exact: true }).click();
  expect(agent.states.get(501)?.runs[0].status).toBe("running");
});

test("等待输入继续订阅，SSE 状态变化刷新标签，后续新 Run 自动接续", async ({ page, agent }, testInfo) => {
  agent.states.set(501, snapshot(1, "waiting_for_input", "等待确认内容"));
  await open(page, [501]);
  await expect.poll(() => agent.attaches.get(501)).toBe(1);
  await expect(tab(page, 501)).toHaveAccessibleName("等待确认 Agent501");
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
  agent.states.set(501, snapshot(4, "running", "新轮次内容"));
  await expect(page.getByText("新轮次内容", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect.poll(() => agent.attaches.get(501)).toBe(2);
});

test("同一个已停止 Run 恢复时重新读取并 attach", async ({ page, agent }) => {
  agent.states.set(501, snapshot(1, "cancelled", "已停止的内容"));
  await open(page, [501]);
  await expect(tab(page, 501)).toHaveAccessibleName("已停止 Agent501");
  await expect(page.getByText("已停止的内容", { exact: true })).toBeVisible();
  expect(agent.attaches.get(501)).toBeUndefined();
  agent.states.set(501, snapshot(1, "running", "恢复后的内容"));
  await expect(page.getByText("恢复后的内容", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect.poll(() => agent.attaches.get(501)).toBe(1);
});

test("摘要读取失败有界且保留旧状态，404 停止观察", async ({ page, agent }) => {
  await open(page, [501, 502]);
  await expect(tab(page, 501)).toHaveAccessibleName("运行中 Agent501");
  agent.errors.set(501, 500);
  const before = agent.reads.get(501)!;
  await expect(tab(page, 501)).toHaveAccessibleName("状态未同步，上次为运行中 Agent501", { timeout: 12_000 });
  expect(agent.reads.get(501)).toBe(before + 3);
  await page.waitForTimeout(3_300);
  expect(agent.reads.get(501)).toBe(before + 3);
  agent.errors.set(501, 404);
  await page.getByRole("button", { name: "收起工作台", exact: true }).click();
  await page.getByRole("button", { name: /工作台，2 个标签，点击打开/ }).click();
  await expect(tab(page, 501)).toHaveAccessibleName("任务已不存在 Agent501");
  const missingCount = agent.reads.get(501);
  await page.waitForTimeout(3_300);
  expect(agent.reads.get(501)).toBe(missingCount);
});

test("意外 EOF 只重同步两次，手动重试恢复完整预算", async ({ page, agent }) => {
  agent.dropStreams = true;
  await open(page, [501]);
  const retry = page.getByRole("button", { name: "重新同步", exact: true });
  await expect(retry).toBeVisible();
  expect(agent.attaches.get(501)).toBe(3);
  await page.waitForTimeout(3_300);
  expect(agent.attaches.get(501)).toBe(3);
  await retry.click();
  await expect.poll(() => agent.attaches.get(501)).toBe(6);
  await expect(retry).toBeVisible();
});


test("窗口隐藏暂停标签查询，可见时刷新且不重建阅读页", async ({ page, agent }) => {
  await open(page, [501]);
  await expect.poll(() => agent.attaches.get(501)).toBe(1);
  await expect(tab(page, 501)).toHaveAccessibleName("运行中 Agent501");
  const snapshots = agent.snapshots.get(501);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const count = agent.reads.get(501);
  await page.waitForTimeout(3_300);
  expect(agent.reads.get(501)).toBe(count);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => agent.reads.get(501)).toBeGreaterThan(count!);
  expect(agent.snapshots.get(501)).toBe(snapshots);
  expect(agent.attaches.get(501)).toBe(1);
});
