import { expect, test } from "@playwright/test";

const emptyState = () => ({
  messages: [],
  run: { runId: null, status: "idle" },
  approvals: {},
  context_usage: 0,
  context_revision: null,
  usage_run_id: null,
  context_usage_used: null,
  context_window_total: null,
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

test("工具追踪视觉回归：Reasoning 和工具组都有图标且完成后收起", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-tool-trace");
  await page.goto("/tasks/42");

  const assistant = page.locator('[data-role="assistant"]').last();
  await expect(assistant.getByText("Reasoning", { exact: true })).toBeVisible();
  await expect(assistant.getByText("2 个工具调用", { exact: true })).toBeVisible();
  await expect(assistant.locator('[data-slot="reasoning-trigger-icon"]')).toHaveCount(1);
  await expect(assistant.locator('[data-slot="tool-group-trigger-icon"]')).toHaveCount(1);
  await expect(assistant.getByText("先分析项目结构", { exact: true })).toHaveCount(0);
  await expect(assistant.getByText("读取文件", { exact: true })).toHaveCount(0);
  await expect(assistant).toHaveScreenshot("assistant-tool-trace.png", { animations: "disabled" });

  await assistant.getByRole("button", { name: "2 个工具调用" }).click();
  await expect(assistant.getByText("读取文件", { exact: true })).toBeVisible();
  await expect(assistant.getByText("搜索文件", { exact: true })).toBeVisible();
  await expect(assistant.getByText(/未知工具/)).toHaveCount(0);
});

test("Reasoning 在真实流式完成后自动收起且仍可手动展开", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "Reasoning 状态回归" } });
  await page.goto("/tasks/42");
  await expect(page.getByLabel("消息输入")).toBeVisible();

  await page.getByLabel("消息输入").fill("验证 reasoning 生命周期");
  await page.getByRole("button", { name: "发送" }).click();

  const assistant = page.locator('[data-role="assistant"]').last();
  await expect(assistant.getByText("Reasoning", { exact: true })).toBeVisible();
  await expect(assistant.getByText(/正在推理/)).toBeVisible();
  await expect(assistant.locator('[data-slot="reasoning-content"]')).toBeVisible();

  await expect(assistant.getByText("streaming response", { exact: true })).toBeVisible({ timeout: 5_000 });
  await expect.poll(async () => {
    const response = await request.get("http://127.0.0.1:8000/__test__/telemetry");
    const telemetry = await response.json() as { completedStreamCount: number };
    return telemetry.completedStreamCount;
  }).toBeGreaterThan(0);
  await expect(assistant.getByText("推理完成", { exact: true })).toHaveCount(0);

  await assistant.getByRole("button", { name: "Reasoning" }).click();
  await expect(assistant.getByText("推理完成", { exact: true })).toBeVisible();
});

test("连续 Run 的 context meter 和 usage footer 只显示当前 Run", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "用量展示回归" } });
  await page.goto("/tasks/42");
  const input = page.getByLabel("消息输入");
  const contextMeter = page.getByTestId("task-context-usage");
  await expect(contextMeter).toHaveAttribute("aria-label", "上下文 —");
  await contextMeter.focus();
  await contextMeter.press("Enter");
  const unknownProgress = page.getByRole("progressbar", { name: "上下文窗口占用" });
  await expect(unknownProgress).not.toHaveAttribute("aria-valuenow");
  await expect(unknownProgress).toHaveAttribute("aria-valuetext", "尚未完成有效测量");
  await page.keyboard.press("Escape");
  await contextMeter.hover();
  await expect(unknownProgress).toBeVisible();
  await page.mouse.move(10, 10);
  await expect(unknownProgress).toBeHidden();

  await input.fill("usage-regression-first");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(contextMeter).toContainText("上下文 70%");
  await expect(page.getByTestId("run-usage-display")).toContainText("本次用量 1.5k tokens");
  await page.getByTestId("run-usage-display").hover();
  const cacheMissRow = page.locator("dt").filter({ hasText: "缓存未命中" }).locator("xpath=following-sibling::dd[1]");
  await expect(cacheMissRow).toHaveText("—");
  await page.mouse.move(10, 10);
  await expect(contextMeter).toHaveAttribute("aria-label", "上下文 70%");
  await contextMeter.focus();
  await expect(contextMeter).toBeFocused();
  await contextMeter.press("Enter");
  const contextProgress = page.getByRole("progressbar", { name: "上下文窗口占用" });
  await expect(contextProgress).toBeVisible();
  await expect(contextProgress).toHaveAttribute("aria-valuetext", "上下文 70%");
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 360, height: 720 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);

  await input.fill("usage-regression-second");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(contextMeter).toContainText("上下文 80%");
  await expect(page.getByTestId("run-usage-display")).toContainText("本次用量 2.5k tokens");
  await expect(page.getByTestId("run-usage-display")).toHaveCount(1);
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
    window.localStorage.setItem("cosir:model-selection:task:42", JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }));
    window.localStorage.setItem("cosir:model-selection:task:43", JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }));
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

test("同一侧栏内切换 Task 不会重新加载整个侧栏", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "任务 A" } });
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 43, title: "任务 B" } });

  let workspaceRequestCount = 0;
  let workspaceTaskRequestCount = 0;
  await page.route("http://127.0.0.1:8000/workspaces", async (route) => {
    workspaceRequestCount += 1;
    await route.continue();
  });
  await page.route("http://127.0.0.1:8000/workspaces/7/tasks", async (route) => {
    workspaceTaskRequestCount += 1;
    await route.continue();
  });
  await page.route("http://127.0.0.1:8000/models", (route) => route.fulfill({ json: [{ provider_id: 2, provider_name: "demo", models: [{ model_name: "demo-model", supports_thinking: false, supports_image: false, supports_video: false, supports_reasoning_effort: false }] }] }));
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem("cosir:model-selection:task:42", JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }));
    window.localStorage.setItem("cosir:model-selection:task:43", JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }));
  });

  await page.goto("/tasks/42");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  const initialWorkspaceRequestCount = workspaceRequestCount;
  const initialWorkspaceTaskRequestCount = workspaceTaskRequestCount;

  await page.locator('nav[aria-label="工作区任务列表"] button').filter({ hasText: "任务 B" }).click();
  await expect(page).toHaveURL(/\/tasks\/43$/);
  await expect(page.getByLabel("消息输入")).toBeVisible();
  expect(workspaceRequestCount).toBe(initialWorkspaceRequestCount);
  expect(workspaceTaskRequestCount).toBe(initialWorkspaceTaskRequestCount);
});

test("Assistant Transport 会展示后端结构化错误", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 103, title: "错误回归任务" } });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem("cosir:model-selection:task:103", JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }));
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
    window.localStorage.setItem("cosir:model-selection:task:43", JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }));
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
