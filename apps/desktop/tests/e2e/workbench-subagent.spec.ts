import { expect, test } from "@playwright/test";

type FrontendLog = {
  event?: string;
  data?: Record<string, unknown>;
};

test.beforeEach(async ({ request }) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
  await request.post("http://127.0.0.1:8000/__test__/seed-delegation");
});

async function frontendLogs(page: import("@playwright/test").Page): Promise<FrontendLog[]> {
  return page.evaluate(() => {
    const value = (window as unknown as { __cosirFrontendLogs?: FrontendLog[] }).__cosirFrontendLogs;
    return value ?? [];
  });
}

test("主 Thread 委派后可在 Workbench 打开子 Agent 并接收只读流", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.clear());
  const attachRequests: string[] = [];
  const businessWrites: string[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/tasks/501/assistant/attach") && request.method() === "POST") attachRequests.push(request.url());
    if (request.method() === "POST" && (/\/tasks\/\d+\/(messages|resume)|\/tool-calls\/[^/]+\/decision/.test(request.url()) || request.url().includes("/cancel"))) businessWrites.push(request.url());
  });

  await page.goto("/tasks/42");
  await expect(page.getByText("子 Agent 已开始工作。", { exact: true })).toBeVisible();
  const row = page.getByTestId("tool-activity-row").filter({ hasText: "审查代码" });
  await expect(row).toContainText("Reviewer");
  // 委派卡片不可展开；标题按钮铺满卡片，点卡片即打开工作台。
  const openReviewer = page.getByRole("button", { name: "打开子 Agent工作台：审查代码" });
  await openReviewer.click();

  await expect(page.getByRole("complementary", { name: "工作台" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /审查代码/ })).toBeVisible();
  await expect.poll(() => attachRequests.length).toBe(1);
  await expect(page.getByText("resumed response", { exact: true })).toBeVisible();
  expect((await frontendLogs(page)).filter((entry) => entry.event === "react_recoverable_render_error")).toEqual([]);
  const workbench = page.getByRole("complementary", { name: "工作台" });
  await expect(workbench.getByRole("button", { name: "发送", exact: true })).toHaveCount(0);
  await expect(workbench.getByRole("button", { name: "编辑并重跑" })).toHaveCount(0);
  await expect(workbench.getByRole("button", { name: "从此处 Fork 新任务" })).toHaveCount(0);
  await expect(workbench.getByRole("button", { name: "关闭 审查代码" })).toBeVisible();
  expect(businessWrites).toEqual([]);
  await workbench.getByRole("button", { name: "关闭 审查代码" }).click();
  await expect(page.getByRole("complementary", { name: "工作台" })).toHaveCount(0);
  await openReviewer.click();
  await expect(page.getByRole("complementary", { name: "工作台" })).toBeVisible();
  await expect(page.getByText("resumed response", { exact: true })).toBeVisible();
  // 只读阅读页随标签挂载常驻订阅子 Task：关闭即释放，重新打开再订阅一次，不触发任何业务写入。
  await expect.poll(() => attachRequests.length).toBe(2);
  expect(businessWrites).toEqual([]);
});

test("两个并行子 Agent 分别订阅，切换回标签页重新订阅并保留内容", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.clear());
  const attachRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/assistant/attach") && request.method() === "POST") attachRequests.push(request.url());
  });

  await page.goto("/tasks/42");
  await expect(page.getByText("子 Agent 已开始工作。", { exact: true })).toBeVisible();
  const reviewer = page.getByRole("button", { name: "打开子 Agent工作台：审查代码" });
  const tester = page.getByRole("button", { name: "打开子 Agent工作台：检查测试" });
  await reviewer.click();
  await expect(page.getByRole("tab", { name: /审查代码/ })).toBeVisible();
  await expect.poll(() => attachRequests.filter((url) => url.includes("/tasks/501/")).length).toBe(1);

  await tester.click();
  await expect(page.getByRole("tab", { name: /检查测试/ })).toBeVisible();
  await expect.poll(() => attachRequests.filter((url) => url.includes("/tasks/502/")).length).toBe(1);
  await expect(page.getByText("resumed response", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: /审查代码/ }).click();
  await expect(page.getByText("resumed response", { exact: true })).toBeVisible();
  expect((await frontendLogs(page)).filter((entry) => entry.event === "react_recoverable_render_error")).toEqual([]);
  // 只有激活标签挂载阅读页：切回 501 时重新订阅一次，502 不受影响。
  await expect.poll(() => attachRequests.filter((url) => url.includes("/tasks/501/")).length).toBe(2);
  expect(attachRequests.filter((url) => url.includes("/tasks/502/")).length).toBe(1);
});
