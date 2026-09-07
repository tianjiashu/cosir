import { expect, test } from "@playwright/test";

const emptyState = () => ({
  messages: [],
  run: { runId: null, status: "idle" },
  approvals: {},
  context_usage: 0,
  usage: {
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cache_hit_tokens: 0,
    cache_miss_tokens: 0,
    reasoning_tokens: 0,
  },
  error: null,
});

test.beforeEach(async ({ request }) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
});

test("直接打开不存在的 Task 会显示错误而不是永久等待", async ({ page }) => {
  await page.goto("/tasks/9999");

  await expect(page.getByText("task not found", { exact: true })).toBeVisible();
  await expect(page.getByText("正在加载任务工作区…", { exact: true })).toHaveCount(0);
});

test("侧栏任务列表失败时不阻塞已确认 Task 的 Assistant", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 104, title: "侧栏失败任务" } });
  await page.route("http://127.0.0.1:8000/workspaces/7/tasks", (route) => route.fulfill({ status: 503, body: "sidebar unavailable" }));

  await page.goto("/tasks/104");

  await expect(page.getByLabel("消息输入")).toBeVisible();
  await expect(page.locator('nav[aria-label="工作区任务列表"]')).toContainText("加载失败");
});

test("侧栏点击 Task 会同步 Tauri WebView 内部路由", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "路由回归任务" } });
  await page.addInitScript(() => window.localStorage.clear());

  await page.goto("/");
  await page.locator('nav[aria-label="工作区任务列表"] button').filter({ hasText: "路由回归任务" }).click();

  await expect(page).toHaveURL(/\/tasks\/42$/);
});

test("跨 Workspace 切换 Task 后请求使用新 Task 所属的 workspaceId", async ({ page }) => {
  const workspaces = [
    { workspace_id: 7, name: "workspace-a", root_path: "C:/a", created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" },
    { workspace_id: 8, name: "workspace-b", root_path: "C:/b", created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" },
  ];
  await page.route("http://127.0.0.1:8000/workspaces", (route) => route.fulfill({ json: workspaces }));
  await page.route("http://127.0.0.1:8000/workspaces/7/tasks", (route) => route.fulfill({ json: [{ task_id: 42, workspace_id: 7, title: "任务 A", execution_status: null, created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" }] }));
  await page.route("http://127.0.0.1:8000/workspaces/8/tasks", (route) => route.fulfill({ json: [{ task_id: 43, workspace_id: 8, title: "任务 B", execution_status: null, created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" }] }));
  await page.route(/http:\/\/127\.0\.0\.1:8000\/tasks\/(42|43)$/, (route) => { const taskId = Number(route.request().url().split("/").at(-1)); return route.fulfill({ json: { task_id: taskId, workspace_id: taskId === 43 ? 8 : 7, title: "任务", task_type: "user", fork_available: true, execution_status: null, created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" } }); });
  await page.route(/http:\/\/127\.0\.0\.1:8000\/tasks\/(42|43)\/assistant\/state/, (route) => route.fulfill({ json: emptyState() }));
  await page.route("http://127.0.0.1:8000/models", (route) => route.fulfill({ json: [{ provider_id: 2, provider_name: "demo", models: [{ model_name: "demo-model", supports_thinking: false, supports_image: false, supports_video: false, supports_reasoning_effort: false }] }] }));
  const requestBodies: Array<Record<string, unknown>> = [];
  await page.route("http://127.0.0.1:8000/assistant", async (route) => {
    requestBodies.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ error: { code: "TASK_WORKSPACE_MISMATCH", message: "mismatch", retryable: false } }) });
  });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem("cosir:model-selection:42", JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }));
    window.localStorage.setItem("cosir:model-selection:43", JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }));
  });

  await page.goto("/");
  await page.locator('nav[aria-label="工作区任务列表"] button').filter({ hasText: "任务 A" }).click();
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await page.locator('nav[aria-label="工作区任务列表"] button').filter({ hasText: "任务 B" }).click();
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await page.getByLabel("消息输入").fill("跨工作区请求");
  await page.getByRole("button", { name: "发送" }).click();

  await expect.poll(() => requestBodies.length).toBeGreaterThan(0);
  expect(requestBodies.every((body) => body.taskId === 43 && body.workspaceId === 8)).toBe(true);
});

test("Assistant Transport 会展示后端结构化错误", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 103, title: "错误回归任务" } });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem("cosir:model-selection:103", JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }));
  });
  await page.route("http://127.0.0.1:8000/assistant", (route) => route.fulfill({
    status: 400,
    contentType: "application/json",
    body: JSON.stringify({ detail: { error: { code: "MODEL_SELECTION_REQUIRED", message: "请先选择模型和模型提供商", retryable: false } } }),
  }));

  await page.goto("/tasks/103");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await page.getByLabel("消息输入").fill("触发结构化错误");
  await page.getByRole("button", { name: "发送" }).click();

  const status = page.getByRole("status");
  await expect(status).toContainText("请先选择模型和模型提供商");
  await expect(status).toHaveClass(/border-destructive/);
  await expect(status).not.toHaveClass(/border-amber/);
});

test("直接路由切换到另一个 Workspace 的 Task 会等待新 Workspace 解析", async ({ page }) => {
  let workspaceLoadCount = 0;
  const workspaceA = { workspace_id: 7, name: "workspace-a", root_path: "C:/a", created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" };
  const workspaceB = { workspace_id: 8, name: "workspace-b", root_path: "C:/b", created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" };
  await page.route("http://127.0.0.1:8000/workspaces", async (route) => {
    workspaceLoadCount += 1;
    if (workspaceLoadCount === 1) return route.fulfill({ json: [workspaceA] });
    await new Promise((resolve) => setTimeout(resolve, 300));
    return route.fulfill({ json: [workspaceA, workspaceB] });
  });
  await page.route("http://127.0.0.1:8000/workspaces/7/tasks", (route) => route.fulfill({ json: [{ task_id: 42, workspace_id: 7, title: "任务 A", execution_status: null, created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" }] }));
  await page.route("http://127.0.0.1:8000/workspaces/8/tasks", (route) => route.fulfill({ json: [{ task_id: 43, workspace_id: 8, title: "任务 B", execution_status: null, created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" }] }));
  await page.route(/http:\/\/127\.0\.0\.1:8000\/tasks\/(42|43)$/, (route) => { const taskId = Number(route.request().url().split("/").at(-1)); return route.fulfill({ json: { task_id: taskId, workspace_id: taskId === 43 ? 8 : 7, title: "任务", task_type: "user", fork_available: true, execution_status: null, created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" } }); });
  await page.route(/http:\/\/127\.0\.0\.1:8000\/tasks\/(42|43)\/assistant\/state/, (route) => route.fulfill({ json: emptyState() }));
  await page.route("http://127.0.0.1:8000/models", (route) => route.fulfill({ json: [{ provider_id: 2, provider_name: "demo", models: [{ model_name: "demo-model", supports_thinking: false, supports_image: false, supports_video: false, supports_reasoning_effort: false }] }] }));
  const requestBodies: Array<Record<string, unknown>> = [];
  await page.route("http://127.0.0.1:8000/assistant", async (route) => {
    requestBodies.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: { error: { code: "TASK_WORKSPACE_MISMATCH", message: "mismatch", retryable: false } } }) });
  });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem("cosir:model-selection:43", JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }));
  });

  await page.goto("/tasks/42");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await page.goto("/tasks/43");
  await page.goto("/tasks/42");
  await page.goto("/tasks/43");
  await expect(page.locator('nav[aria-label="工作区任务列表"] button').filter({ hasText: "任务 B" })).toBeVisible();
  await page.getByLabel("消息输入").fill("直接路由切换");
  await page.getByRole("button", { name: "发送" }).click();

  await expect.poll(() => requestBodies.length).toBeGreaterThan(0);
  expect(requestBodies.every((body) => body.taskId === 43 && body.workspaceId === 8)).toBe(true);
});
