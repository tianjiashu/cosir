import { expect, test } from "@playwright/test";

import type { TransportState } from "../../lib/assistant/contract";
import { seedState } from "./transport-fixtures";
import { API_ORIGIN, OUTPUT_DIR } from "./e2e-env";

const emptyState = (): TransportState => ({ title: "测试对话", messages: [], messageOffset: 0, activity: null, terminal_sessions: [], background_tasks: [], foreground_waits: [], handoffs: [] });

test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
});

test("直接打开不存在的 Task 会显示错误而不是永久等待", async ({ page }) => {
  await page.goto("/tasks/9999");

  await expect(page.getByText("task not found", { exact: true })).toBeVisible();
  await expect(page.getByTestId("task-thread-skeleton")).toHaveCount(0);
});

test("普通发送被 Assistant Transport 拒绝时不显示消息并恢复输入", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 42, title: "拒绝发送回归" } });
  await page.route(`${API_ORIGIN}/tasks/42/messages`, async (route) => {
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
      JSON.stringify({ modelConfigId: "demo/demo-model", reasoningEffort: null }),
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

test("回合结束后多次调用收进过程区，展开后按顺序出现", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/seed-tool-trace`);
  await page.goto("/tasks/42");

  const assistant = page.locator('[data-role="assistant"]').last();
  const head = assistant.locator('[data-slot="process-head"]');
  await expect(head).toBeVisible();
  await expect(assistant.locator('[data-slot="turn-status"]')).not.toContainText(/读取文件|搜索内容|次/);
  await expect(assistant.getByText("读取文件", { exact: true })).toHaveCount(0);

  await head.click();
  await assistant.locator('[data-slot="activity-head"]').click();
  await expect(assistant.getByText("读取文件", { exact: true })).toBeVisible();
  await expect(assistant.getByText("搜索内容", { exact: true })).toBeVisible();
  await expect(assistant.getByText(/未知工具/)).toHaveCount(0);
  // 成功不画对勾。
  await expect(assistant.locator(".lucide-check")).toHaveCount(0);
});

test("网页搜索结果只显示标题并保留标题链接", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/seed-web-search`);
  await page.goto("/tasks/42");

  const assistant = page.locator('[data-role="assistant"]').last();
  // 没有最终回复时过程保持展开；调用收在活动摘要里，按需展开具体结果。
  await assistant.locator('[data-slot="activity-head"]').click();
  await assistant.locator('button[data-slot="call-row"]').click();
  const resultLink = assistant.getByRole("link", { name: "Assistant UI 官方文档" });
  await expect(resultLink).toBeVisible();
  await expect(resultLink).toHaveAttribute("href", "https://assistant-ui.com/docs");
  await expect(assistant.getByText("这段摘要不应出现在搜索结果 UI 中", { exact: true })).toHaveCount(0);
  await expect(assistant.getByText("https://assistant-ui.com/docs", { exact: true })).toHaveCount(0);
});

test("Mermaid 时序图全屏后仍保留可见 SVG", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/seed-mermaid`);
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

test("工具运行中保持单行，完成拿到展示数据后才可展开且默认收起", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 42, title: "工具生命周期回归" } });
  await page.goto("/tasks/42");
  await page.getByLabel("消息输入").fill("tool-lifecycle");
  await page.getByRole("button", { name: "发送", exact: true }).click();

  const assistant = page.locator('[data-role="assistant"]').last();
  await expect(assistant.getByText("搜索内容", { exact: true })).toBeVisible();
  await assistant.locator('[data-slot="activity-head"]').click();
  // 无 display_data 的执行期不展开空详情，也不渲染「未找到匹配」之类的伪结果。
  await expect(assistant.getByText("未找到匹配", { exact: true })).toHaveCount(0);
  // Fork belongs to inputs now; use canonical activity to wait for the terminal outcome.
  await expect.poll(async () => (await (await request.get(`${API_ORIGIN}/tasks/42/assistant/state`)).json()).activity.status).toBe("completed");
  const toolRow = assistant.locator('button[data-slot="call-row"]');
  await expect(toolRow).toBeVisible({ timeout: 3_000 });
  // 命中行拆成可点开的「路径:行号」与高亮正文两部分。
  const match = assistant.getByText("lifecycle.test.ts:1", { exact: true });
  await expect(match).toBeHidden();
  await toolRow.click();
  await expect(match).toBeVisible();
});

test("失败的工具在流结束后显示失败而不是执行中", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 42, title: "失败工具生命周期回归" } });
  await page.goto("/tasks/42");
  await page.getByLabel("消息输入").fill("tool-lifecycle-failed");
  await page.getByRole("button", { name: "发送", exact: true }).click();

  const assistant = page.locator('[data-role="assistant"]').last();
  await expect(assistant.getByText("搜索内容", { exact: true })).toBeVisible();
  await assistant.locator('[data-slot="activity-head"]').click();
  await expect(assistant.getByText("执行失败", { exact: true })).toBeVisible({ timeout: 3_000 });
  await expect(assistant.locator('[data-call-status="failed"]')).toHaveCount(1);
  await expect(assistant.locator('[data-call-status="running"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: "停止", exact: true })).toHaveCount(0);
});

test("取消的工具在流结束后显示已取消而不是执行中", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 42, title: "取消工具生命周期回归" } });
  await page.goto("/tasks/42");
  await page.getByLabel("消息输入").fill("tool-lifecycle-cancelled");
  await page.getByRole("button", { name: "发送", exact: true }).click();

  const assistant = page.locator('[data-role="assistant"]').last();
  await expect(assistant.getByText("搜索内容", { exact: true })).toBeVisible();
  await assistant.locator('[data-slot="activity-head"]').click();
  await expect(assistant.getByText("已取消", { exact: true }).first()).toBeVisible({ timeout: 3_000 });
  await expect(assistant.locator('[data-call-status="cancelled"]')).toHaveCount(1);
  await expect(assistant.locator('[data-call-status="running"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: "停止", exact: true })).toHaveCount(0);
});

const SHOTS = OUTPUT_DIR;
/** 对照截图用高视口：吸底的输入框不盖住回合。 */
const SHOT_VIEWPORT = { width: 960, height: 1400 };

test("交替序列：结束后过程收起带计数，结论、改动卡与等待卡常显，展开后调用行按顺序出现", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/seed-alternating`, { data: { phase: "settled" } });
  await page.setViewportSize(SHOT_VIEWPORT);
  await page.goto("/tasks/42");

  const assistant = page.locator('[data-role="assistant"]').last();
  const head = assistant.locator('[data-slot="process-head"]');
  await expect(assistant.locator('[data-slot="turn-status"]')).toContainText("用时 3分钟12秒");
  await expect(head).toHaveAccessibleName("用时 3分钟12秒，展开过程");
  await expect(assistant.locator('[data-slot="turn-status"]')).not.toContainText(/搜索内容|执行命令|应用补丁|次/);
  // 过程正文在原位置隐藏；尚未展开的调用详情不挂载。
  await expect(assistant.getByText("先找调度循环在哪里。", { exact: true })).toBeHidden();
  await expect(assistant.locator('[data-tool-call-id="alt-search"]')).toHaveCount(0);
  // 结论、改动卡与等待卡常显。
  await expect(assistant.getByText(/已把调度间隔改为读配置项/)).toBeVisible();
  const changes = assistant.locator('[data-slot="changes-card"]');
  await expect(changes).toContainText("涉及 2 个路径");
  await expect(changes).toContainText("1 次调用");
  const attention = assistant.locator('[data-slot="attention-card"]');
  await expect(attention).toContainText("交给你操作网页");
  await expect(attention).toContainText("输入完成后点「交还」，Agent 会继续安装");
  await expect(attention.getByRole("button", { name: "交还" })).toBeVisible();
  await assistant.screenshot({ path: `${SHOTS}/impl-settled.png`, animations: "disabled" });

  await head.click();
  for (const group of await assistant.locator('[data-slot="activity-head"]').all()) await group.click();
  const rows = assistant.locator("[data-tool-call-id]").filter({ has: page.locator('[data-slot="call-row"]') });
  await expect(rows).toHaveCount(4);
  await expect(rows.nth(0)).toContainText("搜索内容");
  await expect(rows.nth(0)).toContainText('"setInterval|tick" in src/');
  await expect(rows.nth(1)).toContainText("运行类型检查");
  await expect(rows.nth(1)).toContainText("命令执行失败");
  await expect(rows.nth(1).locator('[data-call-status="failed"]')).toHaveCount(1);
  await expect(rows.nth(2)).toContainText("应用补丁");
  await expect(rows.nth(2)).toContainText("src/config.ts");
  await expect(rows.nth(3)).toContainText("交给你操作网页");
  await expect(rows.nth(3)).toContainText("输入 sudo 密码");
  await expect(rows.nth(3)).toContainText("等你操作");
  await expect(assistant.getByText("先找调度循环在哪里。", { exact: true })).toBeVisible();
  await assistant.screenshot({ path: `${SHOTS}/impl-expanded.png`, animations: "disabled" });

  // 改动卡点开文件展开 diff。
  await changes.getByRole("button", { name: /src\/config\.ts/ }).click();
  await expect(changes.locator(".aui-diff-view")).toBeVisible();
});

test("交替序列：运行中过程区展开、区头显示当前动作，命令输出跟随主题展开", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/seed-alternating`, { data: { phase: "running" } });
  await page.setViewportSize(SHOT_VIEWPORT);
  await page.goto("/tasks/42");

  const assistant = page.locator('[data-role="assistant"]').last();
  const head = assistant.locator('[data-slot="process-head"]');
  await expect(assistant.locator('[data-slot="current-activity"]')).toContainText("运行单元测试");
  await expect(head).toHaveAttribute("aria-expanded", "true");
  await assistant.locator('[data-slot="activity-head"]').last().click();
  const running = assistant.locator('[data-tool-call-id="alt-test"]');
  await running.locator('[data-slot="call-row"]').click();
  await expect(running).toContainText("bun test tests/task.test.ts");
  await expect(running.locator('[data-terminal-mode="preview"]')).toBeVisible();
  await expect(running).toHaveAttribute("data-tool-status", "running");
  // 复制按钮悬停才出现，触屏与键盘同样可用（见 `REVEAL_ON_HOVER_CLASS`）。
  const copyCommand = running.getByRole("button", { name: "复制命令" });
  await expect(copyCommand).toBeVisible();
  await running.locator('[data-terminal-mode="preview"]').hover();
  await expect(copyCommand).toHaveCSS("opacity", "1");
  await expect(running.getByRole("button", { name: "复制输出" })).toHaveCSS("opacity", "1");
  await page.mouse.move(0, 0);
  await expect(assistant.locator('[data-slot="changes-card"]')).toContainText("涉及 2 个路径");
  await assistant.screenshot({ path: `${SHOTS}/impl-running.png`, animations: "disabled" });
});

test("交替序列：深色主题下回合正常渲染", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/seed-alternating`, { data: { phase: "settled" } });
  await page.addInitScript(() => window.localStorage.setItem("cosir.theme-preference", "dark"));
  await page.setViewportSize(SHOT_VIEWPORT);
  await page.goto("/tasks/42");

  await expect(page.locator("html")).toHaveClass(/dark/);
  const assistant = page.locator('[data-role="assistant"]').last();
  await expect(assistant.locator('[data-slot="process-head"]')).toBeVisible();
  await expect(assistant.locator('[data-slot="changes-card"]')).toBeVisible();
  await expect(assistant.locator('[data-slot="attention-card"]')).toBeVisible();
  await assistant.locator('[data-slot="process-head"]').click();
  await assistant.locator('[data-slot="activity-head"]').first().click();
  await expect(assistant.locator('[data-tool-call-id="alt-search"]')).toBeVisible();
  await assistant.screenshot({ path: `${SHOTS}/impl-dark.png`, animations: "disabled" });
});

test("连续 Run 的 context meter 是 Task 级且每个 Run 都保留 usage footer", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 42, title: "用量展示回归" } });
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
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 104, title: "侧栏失败任务" } });
  await page.route(`${API_ORIGIN}/workspaces/7/tasks`, (route) => route.fulfill({ status: 503, body: "sidebar unavailable" }));

  await page.goto("/tasks/104");

  await expect(page.getByLabel("消息输入")).toBeVisible();
  await expect(page.locator('nav[aria-label="工作区任务列表"]')).toContainText("加载失败");
});

test("侧栏点击 Task 会同步渲染进程内部路由", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 42, title: "路由回归任务" } });
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
  await page.route(`${API_ORIGIN}/workspaces`, (route) => route.fulfill({ json: workspaces }));
  await page.route(`${API_ORIGIN}/workspaces/7/tasks`, (route) => route.fulfill({ json: [{ task_id: 42, workspace_id: 7, title: "任务 A", created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" }] }));
  await page.route(`${API_ORIGIN}/workspaces/8/tasks`, (route) => route.fulfill({ json: [{ task_id: 43, workspace_id: 8, title: "任务 B", created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" }] }));
  await page.route(`${API_ORIGIN}/tasks/{42,43}`, (route) => { const taskId = Number(route.request().url().split("/").at(-1)); return route.fulfill({ json: { task_id: taskId, workspace_id: taskId === 43 ? 8 : 7, title: "任务", task_type: "user", created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" } }); });
  // 对话 state 与 Task 长订阅由模拟后端持有：订阅需要常驻连接，路由夹具无法保持。
  for (const taskId of [42, 43]) await seedState(request, taskId, emptyState());
  await page.route(`${API_ORIGIN}/configuration/model-connections`, (route) => route.fulfill({ json: { path: "/models.json", connections: [{ connection_id: "demo", name: "Demo", base_url: "https://api.example.com", api_key: "test-api-key", models: [{ config_id: "demo/demo-model", connection_id: "demo", model_name: "demo-model", context_window_k: 128, supports_thinking: false, supports_image: false, supports_reasoning_effort: false }] }] } }));
  // Task 由路径寻址，workspace 归属由后端从 Task 事实解析；这里只需核对请求命中的 Task。
  const requestUrls: string[] = [];
  await page.route(`${API_ORIGIN}/tasks/*/messages`, async (route) => {
    requestUrls.push(route.request().url());
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ error: { code: "TASK_WORKSPACE_MISMATCH", message: "mismatch", retryable: false } }) });
  });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem("cosir:model-config-selection:task:42", JSON.stringify({ modelConfigId: "demo/demo-model", reasoningEffort: null }));
    window.localStorage.setItem("cosir:model-config-selection:task:43", JSON.stringify({ modelConfigId: "demo/demo-model", reasoningEffort: null }));
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
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 42, title: "任务 A" } });
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 43, title: "任务 B" } });

  let workspaceRequestCount = 0;
  let workspaceTaskRequestCount = 0;
  await page.route(`${API_ORIGIN}/workspaces`, async (route) => {
    workspaceRequestCount += 1;
    await route.continue();
  });
  await page.route(`${API_ORIGIN}/workspaces/7/tasks`, async (route) => {
    workspaceTaskRequestCount += 1;
    await route.continue();
  });
  await page.route(`${API_ORIGIN}/configuration/model-connections`, (route) => route.fulfill({ json: { path: "/models.json", connections: [{ connection_id: "demo", name: "Demo", base_url: "https://api.example.com", api_key: "test-api-key", models: [{ config_id: "demo/demo-model", connection_id: "demo", model_name: "demo-model", context_window_k: 128, supports_thinking: false, supports_image: false, supports_reasoning_effort: false }] }] } }));
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem("cosir:model-config-selection:task:42", JSON.stringify({ modelConfigId: "demo/demo-model", reasoningEffort: null }));
    window.localStorage.setItem("cosir:model-config-selection:task:43", JSON.stringify({ modelConfigId: "demo/demo-model", reasoningEffort: null }));
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
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 103, title: "错误回归任务" } });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem("cosir:model-config-selection:task:103", JSON.stringify({ modelConfigId: "demo/demo-model", reasoningEffort: null }));
  });
  await page.route(`${API_ORIGIN}/tasks/103/messages`, (route) => route.fulfill({
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
  await page.route(`${API_ORIGIN}/workspaces`, async (route) => {
    workspaceLoadCount += 1;
    if (workspaceLoadCount === 1) return route.fulfill({ json: [workspaceA] });
    await new Promise((resolve) => setTimeout(resolve, 300));
    return route.fulfill({ json: [workspaceA, workspaceB] });
  });
  await page.route(`${API_ORIGIN}/workspaces/7/tasks`, (route) => route.fulfill({ json: [{ task_id: 42, workspace_id: 7, title: "任务 A", created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" }] }));
  await page.route(`${API_ORIGIN}/workspaces/8/tasks`, (route) => route.fulfill({ json: [{ task_id: 43, workspace_id: 8, title: "任务 B", created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" }] }));
  await page.route(`${API_ORIGIN}/tasks/{42,43}`, (route) => { const taskId = Number(route.request().url().split("/").at(-1)); return route.fulfill({ json: { task_id: taskId, workspace_id: taskId === 43 ? 8 : 7, title: "任务", task_type: "user", created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z" } }); });
  // 对话 state 与 Task 长订阅由模拟后端持有：订阅需要常驻连接，路由夹具无法保持。
  for (const taskId of [42, 43]) await seedState(request, taskId, emptyState());
  await page.route(`${API_ORIGIN}/configuration/model-connections`, (route) => route.fulfill({ json: { path: "/models.json", connections: [{ connection_id: "demo", name: "Demo", base_url: "https://api.example.com", api_key: "test-api-key", models: [{ config_id: "demo/demo-model", connection_id: "demo", model_name: "demo-model", context_window_k: 128, supports_thinking: false, supports_image: false, supports_reasoning_effort: false }] }] } }));
  // Task 由路径寻址，workspace 归属由后端从 Task 事实解析；这里只需核对请求命中的 Task。
  const requestUrls: string[] = [];
  await page.route(`${API_ORIGIN}/tasks/*/messages`, async (route) => {
    requestUrls.push(route.request().url());
    await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: { error: { code: "TASK_WORKSPACE_MISMATCH", message: "mismatch", retryable: false } } }) });
  });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem("cosir:model-config-selection:task:43", JSON.stringify({ modelConfigId: "demo/demo-model", reasoningEffort: null }));
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
