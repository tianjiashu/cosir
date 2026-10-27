import { expect, test } from "@playwright/test";

import type { TransportState } from "../../lib/assistant/contract";
import { seedState } from "./transport-fixtures";

const emptyState = (): TransportState => ({ title: "测试对话", messages: [], activity: null });

test.beforeEach(async ({ request }) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
});

test("直接打开不存在的 Task 会显示错误而不是永久等待", async ({ page }) => {
  await page.goto("/tasks/9999");

  await expect(page.getByText("task not found", { exact: true })).toBeVisible();
  await expect(page.getByTestId("task-thread-skeleton")).toHaveCount(0);
});

test("普通发送被 Assistant Transport 拒绝时不显示消息并恢复输入", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "拒绝发送回归" } });
  await page.route("http://127.0.0.1:8000/tasks/42/messages", async (route) => {
    await route.fulfill({
      status: 400,
      contentType: "application/json",
      body: JSON.stringify({ detail: { error: { code: "INVALID_MESSAGE", message: "测试拒绝普通发送", retryable: true } } }),
    });
  });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-config-selection:task:42",
      JSON.stringify({ modelConfigId: 2, reasoningEffort: null }),
    );
  });

  await page.goto("/tasks/42");
  const input = page.getByLabel("消息输入");
  await input.fill("这条消息不能进入对话");
  await page.getByRole("button", { name: "发送", exact: true }).click();

  await expect(page.getByRole("dialog")).toContainText("测试拒绝普通发送");
  await expect(input).toHaveText("这条消息不能进入对话");
  await expect(page.locator('[data-role="user"]').getByText("这条消息不能进入对话", { exact: true })).toHaveCount(0);
});

test("工具追踪视觉回归：Reasoning 和工具组都有图标且完成后收起", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-tool-trace");
  await page.goto("/tasks/42");

  const assistant = page.locator('[data-role="assistant"]').last();
  await expect(assistant.getByText("思考过程", { exact: true })).toBeVisible();
  await expect(assistant.getByText("2 个工具调用 · 全部成功", { exact: true })).toBeVisible();
  await expect(assistant.locator('[data-slot="reasoning-trigger-icon"]')).toHaveCount(1);
  await expect(assistant.locator('[data-slot="tool-group-trigger-icon"]')).toHaveCount(1);
  await expect(assistant.getByText("先分析项目结构", { exact: true })).toHaveCount(0);
  await expect(assistant.getByText("读取文件", { exact: true })).toHaveCount(0);
  await expect(assistant).toHaveScreenshot("assistant-tool-trace.png", { animations: "disabled" });

  await assistant.getByRole("button", { name: /2 个工具调用 · 全部成功/ }).click();
  await expect(assistant.getByText("读取文件", { exact: true })).toBeVisible();
  await expect(assistant.getByText("搜索文件", { exact: true })).toBeVisible();
  await expect(assistant.getByText(/未知工具/)).toHaveCount(0);
});

test("网页搜索结果只显示标题并保留标题链接", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-web-search");
  await page.goto("/tasks/42");

  const assistant = page.locator('[data-role="assistant"]').last();
  const resultLink = assistant.getByRole("link", { name: "Assistant UI 官方文档" });
  await expect(resultLink).toBeVisible();
  await expect(resultLink).toHaveAttribute("href", "https://assistant-ui.com/docs");
  await expect(assistant.getByText("这段摘要不应出现在搜索结果 UI 中", { exact: true })).toHaveCount(0);
  await expect(assistant.getByText("https://assistant-ui.com/docs", { exact: true })).toHaveCount(0);
});

test("Mermaid 时序图全屏后仍保留可见 SVG", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-mermaid");
  await page.goto("/tasks/42");

  const diagram = page.locator('[data-streamdown="mermaid-block"]').last();
  const inlineSvg = diagram.locator('[data-streamdown="mermaid"] [aria-label="Mermaid chart"] > svg');
  await expect(inlineSvg).toHaveCount(1);
  await expect(inlineSvg).toBeVisible();

  await diagram.getByRole("button", { name: "View fullscreen" }).click();

  const fullscreen = page.locator('[data-streamdown="mermaid-fullscreen"]');
  await expect(fullscreen).toBeVisible();
  const chart = fullscreen.locator('[aria-label="Mermaid chart"]');
  await expect.poll(async () => chart.evaluate((element) => getComputedStyle(element).flexDirection)).toBe("column");
  const fullscreenSvg = fullscreen.locator('[aria-label="Mermaid chart"] > svg');
  await expect(fullscreenSvg).toHaveCount(1);
  await expect.poll(async () => fullscreenSvg.evaluate((element) => element.children.length)).toBeGreaterThan(0);
  await expect.poll(async () => fullscreenSvg.evaluate((element) => {
    const { width, height } = element.getBoundingClientRect();
    return width > 0 && height > 0;
  })).toBe(true);
});

test("Reasoning 在真实流式完成后自动收起且仍可手动展开", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "Reasoning 状态回归" } });
  await page.goto("/tasks/42");
  await expect(page.getByLabel("消息输入")).toBeVisible();

  await page.getByLabel("消息输入").fill("验证 reasoning 生命周期");
  await page.getByRole("button", { name: "发送", exact: true }).click();

  const assistant = page.locator('[data-role="assistant"]').last();
  await expect(assistant.getByText("思考过程", { exact: true })).toBeVisible();
  await expect(assistant.getByText(/正在推理/)).toBeVisible();
  await expect(assistant.locator('[data-slot="reasoning-content"]')).toBeVisible();

  await expect(assistant.getByText("streaming response", { exact: true })).toBeVisible({ timeout: 5_000 });
  await expect.poll(async () => {
    const response = await request.get("http://127.0.0.1:8000/__test__/telemetry");
    const telemetry = await response.json() as { completedStreamCount: number };
    return telemetry.completedStreamCount;
  }).toBeGreaterThan(0);
  await expect(assistant.getByText("推理完成", { exact: true })).toHaveCount(0);

  await assistant.getByRole("button", { name: "思考过程" }).click();
  await expect(assistant.getByText("推理完成", { exact: true })).toBeVisible();
});

test("工具运行中保持静态状态行，完成拿到展示数据后才可展开且默认收起", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "工具生命周期回归" } });
  await page.goto("/tasks/42");
  await page.getByLabel("消息输入").fill("tool-lifecycle");
  await page.getByRole("button", { name: "发送", exact: true }).click();

  const assistant = page.locator('[data-role="assistant"]').last();
  await expect(assistant.getByText("搜索文件", { exact: true })).toBeVisible();
  // 无 display_data 的执行期不展开空详情，也不渲染「未找到匹配」之类的伪结果。
  await expect(assistant.getByText("未找到匹配", { exact: true })).toHaveCount(0);
  const toolRow = assistant.getByRole("button", { name: /搜索文件/ });
  await expect(toolRow).toBeVisible({ timeout: 3_000 });
  const match = assistant.getByText("lifecycle.test.ts:1 lifecycle", { exact: true });
  await expect(match).toBeHidden();
  await toolRow.click();
  await expect(match).toBeVisible();
});

test("失败的工具在流结束后显示失败而不是执行中", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "失败工具生命周期回归" } });
  await page.goto("/tasks/42");
  await page.getByLabel("消息输入").fill("tool-lifecycle-failed");
  await page.getByRole("button", { name: "发送", exact: true }).click();

  const assistant = page.locator('[data-role="assistant"]').last();
  await expect(assistant.getByText("搜索文件", { exact: true })).toBeVisible();
  await expect(assistant.getByText("失败", { exact: true })).toBeVisible({ timeout: 3_000 });
  await expect(assistant.getByText("执行中", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "停止" })).toHaveCount(0);
});

test("取消的工具在流结束后显示已取消而不是执行中", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "取消工具生命周期回归" } });
  await page.goto("/tasks/42");
  await page.getByLabel("消息输入").fill("tool-lifecycle-cancelled");
  await page.getByRole("button", { name: "发送", exact: true }).click();

  const assistant = page.locator('[data-role="assistant"]').last();
  await expect(assistant.getByText("搜索文件", { exact: true })).toBeVisible();
  await expect(assistant.getByText("已取消", { exact: true }).first()).toBeVisible({ timeout: 3_000 });
  await expect(assistant.getByText("执行中", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "停止" })).toHaveCount(0);
});

test("连续 Run 的 context meter 是 Task 级且每个 Run 都保留 usage footer", async ({ page, request }) => {
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
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(contextMeter).toContainText("上下文 1%");
  await expect(page.getByTestId("run-usage-display")).toContainText("本次用量 1.5k tokens");
  const cacheMissRow = page.locator("dt").filter({ hasText: "缓存未命中" }).locator("xpath=following-sibling::dd[1]");
  // 流结束后消息页脚仍会因 Reasoning 收起等布局收敛而位移，悬停可能落空，故重试直到浮层打开。
  await expect(async () => {
    await page.getByTestId("run-usage-display").hover();
    await expect(cacheMissRow).toHaveText("—", { timeout: 1_000 });
  }).toPass();
  await page.mouse.move(10, 10);
  await expect(contextMeter).toHaveAttribute("aria-label", "上下文 1%");
  await contextMeter.focus();
  await expect(contextMeter).toBeFocused();
  await contextMeter.press("Enter");
  const contextProgress = page.getByRole("progressbar", { name: "上下文窗口占用" });
  await expect(contextProgress).toBeVisible();
  await expect(contextProgress).toHaveAttribute("aria-valuetext", "上下文 1%");
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 360, height: 720 });
  // 视口收窄后侧栏等响应式布局需要一帧以上才完成重排，轮询最终状态而非瞬时状态。
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);

  await input.fill("usage-regression-second");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(contextMeter).toContainText("上下文 2%");
  await expect(page.getByTestId("run-usage-display").last()).toContainText("本次用量 2.5k tokens");
  await expect(page.getByTestId("run-usage-display")).toHaveCount(2);
  await expect(page.getByTestId("run-usage-display").first()).toContainText("本次用量 1.5k tokens");
  await page.reload();
  await expect(page.getByTestId("run-usage-display")).toHaveCount(2);
  await expect(page.getByTestId("run-usage-display").first()).toContainText("本次用量 1.5k tokens");
  await expect(page.getByTestId("run-usage-display").last()).toContainText("本次用量 2.5k tokens");
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
  await page.getByRole("navigation", { name: "工作区任务列表" }).getByRole("treeitem").filter({ hasText: "路由回归任务" }).click();

  await expect(page).toHaveURL(/\/tasks\/42$/);
});

test("跨 Workspace 切换 Task 后请求使用新 Task 所属的 workspaceId", async ({ page, request }) => {
  const workspaces = [
    { workspace_id: 7, name: "workspace-a", root_path: "C:/a", created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" },
    { workspace_id: 8, name: "workspace-b", root_path: "C:/b", created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" },
  ];
  await page.route("http://127.0.0.1:8000/workspaces", (route) => route.fulfill({ json: workspaces }));
  await page.route("http://127.0.0.1:8000/workspaces/7/tasks", (route) => route.fulfill({ json: [{ task_id: 42, workspace_id: 7, title: "任务 A", execution_status: null, created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" }] }));
  await page.route("http://127.0.0.1:8000/workspaces/8/tasks", (route) => route.fulfill({ json: [{ task_id: 43, workspace_id: 8, title: "任务 B", execution_status: null, created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" }] }));
  await page.route(/http:\/\/127\.0\.0\.1:8000\/tasks\/(42|43)$/, (route) => { const taskId = Number(route.request().url().split("/").at(-1)); return route.fulfill({ json: { task_id: taskId, workspace_id: taskId === 43 ? 8 : 7, title: "任务", task_type: "user", execution_status: null, created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" } }); });
  // 对话 state 与 Task 长订阅由模拟后端持有：订阅需要常驻连接，路由夹具无法保持。
  for (const taskId of [42, 43]) await seedState(request, taskId, emptyState());
  await page.route("http://127.0.0.1:8000/model-configs", (route) => route.fulfill({ json: [{ config_id: 2, config_name: "Demo", base_url: "https://api.example.com", api_key: "test-api-key", model_name: "demo-model", context_window_k: 128, api_key_configured: true, enabled: true, sort_order: 0, created_at: "", updated_at: "", supports_thinking: false, supports_image: false, supports_reasoning_effort: false }] }));
  // Task 由路径寻址，workspace 归属由后端从 Task 事实解析；这里只需核对请求命中的 Task。
  const requestUrls: string[] = [];
  await page.route(/http:\/\/127\.0\.0\.1:8000\/tasks\/\d+\/messages$/, async (route) => {
    requestUrls.push(route.request().url());
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ error: { code: "TASK_WORKSPACE_MISMATCH", message: "mismatch", retryable: false } }) });
  });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem("cosir:model-config-selection:task:42", JSON.stringify({ modelConfigId: 2, reasoningEffort: null }));
    window.localStorage.setItem("cosir:model-config-selection:task:43", JSON.stringify({ modelConfigId: 2, reasoningEffort: null }));
  });

  await page.goto("/");
  await page.getByRole("navigation", { name: "工作区任务列表" }).getByRole("treeitem").filter({ hasText: "任务 A" }).click();
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await page.getByRole("navigation", { name: "工作区任务列表" }).getByRole("treeitem").filter({ hasText: "任务 B" }).click();
  // 等 Task B 的 runtime 挂载后再输入：否则会写进即将卸载的 Task A composer。
  await expect.poll(() => page.evaluate(() => (
    (window as Window & { __cosirFrontendLogs?: { event: string; data?: { taskId?: number } }[] }).__cosirFrontendLogs ?? []
  ).some((entry) => entry.event === "assistant_runtime_mounted" && entry.data?.taskId === 43))).toBe(true);
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await page.getByLabel("消息输入").fill("跨工作区请求");
  await page.getByRole("button", { name: "发送", exact: true }).click();

  await expect.poll(() => requestUrls.length).toBeGreaterThan(0);
  expect(requestUrls.every((url) => url.endsWith("/tasks/43/messages"))).toBe(true);
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
  await page.route("http://127.0.0.1:8000/model-configs", (route) => route.fulfill({ json: [{ config_id: 2, config_name: "Demo", base_url: "https://api.example.com", api_key: "test-api-key", model_name: "demo-model", context_window_k: 128, api_key_configured: true, enabled: true, sort_order: 0, created_at: "", updated_at: "", supports_thinking: false, supports_image: false, supports_reasoning_effort: false }] }));
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem("cosir:model-config-selection:task:42", JSON.stringify({ modelConfigId: 2, reasoningEffort: null }));
    window.localStorage.setItem("cosir:model-config-selection:task:43", JSON.stringify({ modelConfigId: 2, reasoningEffort: null }));
  });

  await page.goto("/tasks/42");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  const initialWorkspaceRequestCount = workspaceRequestCount;
  const initialWorkspaceTaskRequestCount = workspaceTaskRequestCount;

  await page.getByRole("navigation", { name: "工作区任务列表" }).getByRole("treeitem").filter({ hasText: "任务 B" }).click();
  await expect(page).toHaveURL(/\/tasks\/43$/);
  await expect(page.getByLabel("消息输入")).toBeVisible();
  expect(workspaceRequestCount).toBe(initialWorkspaceRequestCount);
  expect(workspaceTaskRequestCount).toBe(initialWorkspaceTaskRequestCount);
});

test("Assistant Transport 会展示后端结构化错误", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 103, title: "错误回归任务" } });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem("cosir:model-config-selection:task:103", JSON.stringify({ modelConfigId: 2, reasoningEffort: null }));
  });
  await page.route("http://127.0.0.1:8000/tasks/103/messages", (route) => route.fulfill({
    status: 400,
    contentType: "application/json",
    body: JSON.stringify({ detail: { error: { code: "MODEL_SELECTION_REQUIRED", message: "请先选择模型和模型提供商", retryable: false } } }),
  }));

  await page.goto("/tasks/103");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await page.getByLabel("消息输入").fill("触发结构化错误");
  await page.getByRole("button", { name: "发送", exact: true }).click();

  // 后端未接受的请求以「发送失败」弹窗展示，而不是可重新同步的连接状态条。
  await expect(page.getByRole("dialog", { name: "发送失败" })).toContainText("请先选择模型和模型提供商");
  await expect(page.getByRole("button", { name: "重新同步" })).toHaveCount(0);
});

test("直接路由切换到另一个 Workspace 的 Task 会等待新 Workspace 解析", async ({ page, request }) => {
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
  await page.route(/http:\/\/127\.0\.0\.1:8000\/tasks\/(42|43)$/, (route) => { const taskId = Number(route.request().url().split("/").at(-1)); return route.fulfill({ json: { task_id: taskId, workspace_id: taskId === 43 ? 8 : 7, title: "任务", task_type: "user", execution_status: null, created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" } }); });
  // 对话 state 与 Task 长订阅由模拟后端持有：订阅需要常驻连接，路由夹具无法保持。
  for (const taskId of [42, 43]) await seedState(request, taskId, emptyState());
  await page.route("http://127.0.0.1:8000/model-configs", (route) => route.fulfill({ json: [{ config_id: 2, config_name: "Demo", base_url: "https://api.example.com", api_key: "test-api-key", model_name: "demo-model", context_window_k: 128, api_key_configured: true, enabled: true, sort_order: 0, created_at: "", updated_at: "", supports_thinking: false, supports_image: false, supports_reasoning_effort: false }] }));
  // Task 由路径寻址，workspace 归属由后端从 Task 事实解析；这里只需核对请求命中的 Task。
  const requestUrls: string[] = [];
  await page.route(/http:\/\/127\.0\.0\.1:8000\/tasks\/\d+\/messages$/, async (route) => {
    requestUrls.push(route.request().url());
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: { error: { code: "TASK_WORKSPACE_MISMATCH", message: "mismatch", retryable: false } } }) });
  });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem("cosir:model-config-selection:task:43", JSON.stringify({ modelConfigId: 2, reasoningEffort: null }));
  });

  await page.goto("/tasks/42");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await page.goto("/tasks/43");
  await page.goto("/tasks/42");
  await page.goto("/tasks/43");
  await expect(page.getByRole("navigation", { name: "工作区任务列表" }).getByRole("treeitem").filter({ hasText: "任务 B" })).toBeVisible();
  await page.getByLabel("消息输入").fill("直接路由切换");
  await page.getByRole("button", { name: "发送", exact: true }).click();

  await expect.poll(() => requestUrls.length).toBeGreaterThan(0);
  expect(requestUrls.every((url) => url.endsWith("/tasks/43/messages"))).toBe(true);
});
