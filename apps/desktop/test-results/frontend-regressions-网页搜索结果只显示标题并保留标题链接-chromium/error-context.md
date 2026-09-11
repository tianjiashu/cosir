# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: frontend-regressions.spec.ts >> 网页搜索结果只显示标题并保留标题链接
- Location: tests\e2e\frontend-regressions.spec.ts:43:5

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: locator('[data-role="assistant"]').last().getByRole('link', { name: 'Assistant UI 官方文档' })
Expected: visible
Timeout: 5000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" with timeout 5000ms
  - waiting for locator('[data-role="assistant"]').last().getByRole('link', { name: 'Assistant UI 官方文档' })

```

```yaml
- complementary:
  - text: 工作区
  - button "收起侧栏"
  - button "新对话"
  - button "刷新工作区"
  - navigation "工作区任务列表":
    - paragraph: 我的工作区
    - button "demo 1"
    - button "更多 demo 操作"
    - button "网页搜索标题展示回归 1/1/2026"
    - button "更多 网页搜索标题展示回归 操作"
- main:
  - paragraph: 对话
  - paragraph: 已存在任务
  - text: demo
  - paragraph: 对话历史加载失败，请重试
  - button "重试"
```

# Test source

```ts
  1   | import { expect, test } from "@playwright/test";
  2   | 
  3   | const emptyState = () => ({
  4   |   runs: [],
  5   |   current_run_id: null,
  6   |   approvals: {},
  7   |   context_usage_ratio: null,
  8   |   context_usage_used: null,
  9   |   context_window_total: null,
  10  |   error: null,
  11  | });
  12  | 
  13  | test.beforeEach(async ({ request }) => {
  14  |   await request.post("http://127.0.0.1:8000/__test__/reset");
  15  | });
  16  | 
  17  | test("直接打开不存在的 Task 会显示错误而不是永久等待", async ({ page }) => {
  18  |   await page.goto("/tasks/9999");
  19  | 
  20  |   await expect(page.getByText("task not found", { exact: true })).toBeVisible();
  21  |   await expect(page.getByText("正在加载任务工作区…", { exact: true })).toHaveCount(0);
  22  | });
  23  | 
  24  | test("工具追踪视觉回归：Reasoning 和工具组都有图标且完成后收起", async ({ page, request }) => {
  25  |   await request.post("http://127.0.0.1:8000/__test__/seed-tool-trace");
  26  |   await page.goto("/tasks/42");
  27  | 
  28  |   const assistant = page.locator('[data-role="assistant"]').last();
  29  |   await expect(assistant.getByText("Reasoning", { exact: true })).toBeVisible();
  30  |   await expect(assistant.getByText("2 个工具调用 · 全部成功", { exact: true })).toBeVisible();
  31  |   await expect(assistant.locator('[data-slot="reasoning-trigger-icon"]')).toHaveCount(1);
  32  |   await expect(assistant.locator('[data-slot="tool-group-trigger-icon"]')).toHaveCount(1);
  33  |   await expect(assistant.getByText("先分析项目结构", { exact: true })).toHaveCount(0);
  34  |   await expect(assistant.getByText("读取文件", { exact: true })).toHaveCount(0);
  35  |   await expect(assistant).toHaveScreenshot("assistant-tool-trace.png", { animations: "disabled" });
  36  | 
  37  |   await assistant.getByRole("button", { name: /2 个工具调用 · 全部成功/ }).click();
  38  |   await expect(assistant.getByText("读取文件", { exact: true })).toBeVisible();
  39  |   await expect(assistant.getByText("搜索文件", { exact: true })).toBeVisible();
  40  |   await expect(assistant.getByText(/未知工具/)).toHaveCount(0);
  41  | });
  42  | 
  43  | test("网页搜索结果只显示标题并保留标题链接", async ({ page, request }) => {
  44  |   await request.post("http://127.0.0.1:8000/__test__/seed-web-search");
  45  |   await page.goto("/tasks/42");
  46  | 
  47  |   const assistant = page.locator('[data-role="assistant"]').last();
  48  |   const resultLink = assistant.getByRole("link", { name: "Assistant UI 官方文档" });
> 49  |   await expect(resultLink).toBeVisible();
      |                            ^ Error: expect(locator).toBeVisible() failed
  50  |   await expect(resultLink).toHaveAttribute("href", "https://assistant-ui.com/docs");
  51  |   await expect(assistant.getByText("这段摘要不应出现在搜索结果 UI 中", { exact: true })).toHaveCount(0);
  52  |   await expect(assistant.getByText("https://assistant-ui.com/docs", { exact: true })).toHaveCount(0);
  53  | });
  54  | 
  55  | test("Reasoning 在真实流式完成后自动收起且仍可手动展开", async ({ page, request }) => {
  56  |   await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "Reasoning 状态回归" } });
  57  |   await page.goto("/tasks/42");
  58  |   await expect(page.getByLabel("消息输入")).toBeVisible();
  59  | 
  60  |   await page.getByLabel("消息输入").fill("验证 reasoning 生命周期");
  61  |   await page.getByRole("button", { name: "发送" }).click();
  62  | 
  63  |   const assistant = page.locator('[data-role="assistant"]').last();
  64  |   await expect(assistant.getByText("Reasoning", { exact: true })).toBeVisible();
  65  |   await expect(assistant.getByText(/正在推理/)).toBeVisible();
  66  |   await expect(assistant.locator('[data-slot="reasoning-content"]')).toBeVisible();
  67  | 
  68  |   await expect(assistant.getByText("streaming response", { exact: true })).toBeVisible({ timeout: 5_000 });
  69  |   await expect.poll(async () => {
  70  |     const response = await request.get("http://127.0.0.1:8000/__test__/telemetry");
  71  |     const telemetry = await response.json() as { completedStreamCount: number };
  72  |     return telemetry.completedStreamCount;
  73  |   }).toBeGreaterThan(0);
  74  |   await expect(assistant.getByText("推理完成", { exact: true })).toHaveCount(0);
  75  | 
  76  |   await assistant.getByRole("button", { name: "Reasoning" }).click();
  77  |   await expect(assistant.getByText("推理完成", { exact: true })).toBeVisible();
  78  | });
  79  | 
  80  | test("工具详情从 pending 进入 running 时自动展开，并在完成后收起", async ({ page, request }) => {
  81  |   await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "工具生命周期回归" } });
  82  |   await page.goto("/tasks/42");
  83  |   await page.getByLabel("消息输入").fill("tool-lifecycle");
  84  |   await page.getByRole("button", { name: "发送" }).click();
  85  | 
  86  |   const assistant = page.locator('[data-role="assistant"]').last();
  87  |   const tool = assistant.getByText("搜索文件", { exact: true });
  88  |   await expect(tool).toBeVisible();
  89  |   const details = assistant.getByText("未找到匹配", { exact: true });
  90  |   await expect(details).toBeVisible({ timeout: 2_000 });
  91  |   await expect(details).toBeHidden({ timeout: 3_000 });
  92  | });
  93  | 
  94  | test("失败的工具在流结束后显示失败而不是执行中", async ({ page, request }) => {
  95  |   await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "失败工具生命周期回归" } });
  96  |   await page.goto("/tasks/42");
  97  |   await page.getByLabel("消息输入").fill("tool-lifecycle-failed");
  98  |   await page.getByRole("button", { name: "发送" }).click();
  99  | 
  100 |   const assistant = page.locator('[data-role="assistant"]').last();
  101 |   await expect(assistant.getByText("搜索文件", { exact: true })).toBeVisible();
  102 |   await expect(assistant.getByText("失败", { exact: true })).toBeVisible({ timeout: 3_000 });
  103 |   await expect(assistant.getByText("执行中", { exact: true })).toHaveCount(0);
  104 |   await expect(page.getByRole("button", { name: "停止" })).toHaveCount(0);
  105 | });
  106 | 
  107 | test("取消的工具在流结束后显示已取消而不是执行中", async ({ page, request }) => {
  108 |   await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "取消工具生命周期回归" } });
  109 |   await page.goto("/tasks/42");
  110 |   await page.getByLabel("消息输入").fill("tool-lifecycle-cancelled");
  111 |   await page.getByRole("button", { name: "发送" }).click();
  112 | 
  113 |   const assistant = page.locator('[data-role="assistant"]').last();
  114 |   await expect(assistant.getByText("搜索文件", { exact: true })).toBeVisible();
  115 |   await expect(assistant.getByRole("button", { name: /搜索文件 已取消/ })).toBeVisible({ timeout: 3_000 });
  116 |   await expect(assistant.getByText("执行中", { exact: true })).toHaveCount(0);
  117 |   await expect(page.getByRole("button", { name: "停止" })).toHaveCount(0);
  118 | });
  119 | 
  120 | test("连续 Run 的 context meter 是 Task 级且每个 Run 都保留 usage footer", async ({ page, request }) => {
  121 |   await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "用量展示回归" } });
  122 |   await page.goto("/tasks/42");
  123 |   const input = page.getByLabel("消息输入");
  124 |   const contextMeter = page.getByTestId("task-context-usage");
  125 |   await expect(contextMeter).toHaveAttribute("aria-label", "上下文 —");
  126 |   await contextMeter.focus();
  127 |   await contextMeter.press("Enter");
  128 |   const unknownProgress = page.getByRole("progressbar", { name: "上下文窗口占用" });
  129 |   await expect(unknownProgress).not.toHaveAttribute("aria-valuenow");
  130 |   await expect(unknownProgress).toHaveAttribute("aria-valuetext", "尚未完成有效测量");
  131 |   await page.keyboard.press("Escape");
  132 |   await contextMeter.hover();
  133 |   await expect(unknownProgress).toBeVisible();
  134 |   await page.mouse.move(10, 10);
  135 |   await expect(unknownProgress).toBeHidden();
  136 | 
  137 |   await input.fill("usage-regression-first");
  138 |   await page.getByRole("button", { name: "发送" }).click();
  139 |   await expect(contextMeter).toContainText("上下文 70%");
  140 |   await expect(page.getByTestId("run-usage-display")).toContainText("本次用量 1.5k tokens");
  141 |   await page.getByTestId("run-usage-display").hover();
  142 |   const cacheMissRow = page.locator("dt").filter({ hasText: "缓存未命中" }).locator("xpath=following-sibling::dd[1]");
  143 |   await expect(cacheMissRow).toHaveText("—");
  144 |   await page.mouse.move(10, 10);
  145 |   await expect(contextMeter).toHaveAttribute("aria-label", "上下文 70%");
  146 |   await contextMeter.focus();
  147 |   await expect(contextMeter).toBeFocused();
  148 |   await contextMeter.press("Enter");
  149 |   const contextProgress = page.getByRole("progressbar", { name: "上下文窗口占用" });
```