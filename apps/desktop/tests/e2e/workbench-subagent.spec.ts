import { expect, test, type Page } from "@playwright/test";

import { frontendLogs, openedSubscriptionCount } from "./frontend-logs";
import { API_ORIGIN } from "./e2e-env";

test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-delegation`);
});

/** 子 Agent 只读阅读页已建立的订阅（501 审查代码、502 检查测试），口径见 `openedSubscriptionCount`。 */
const WORKBENCH_SUBSCRIPTION = "workbench_agent_subscription_opened";
const reviewerSubscriptions = (page: Page) => openedSubscriptionCount(page, { event: WORKBENCH_SUBSCRIPTION, taskId: 501 });
const testerSubscriptions = (page: Page) => openedSubscriptionCount(page, { event: WORKBENCH_SUBSCRIPTION, taskId: 502 });

test("主 Thread 委派后可在 Workbench 打开子 Agent 并接收只读流", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.clear());
  const businessWrites: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && (/\/tasks\/\d+\/(messages|resume)|\/tool-calls\/[^/]+\/decision/.test(request.url()) || request.url().includes("/cancel"))) businessWrites.push(request.url());
  });

  await page.goto("/tasks/42");
  await expect(page.getByText("子 Agent 已开始工作。", { exact: true })).toBeVisible();
  // 回合已结束，两次委派收在过程区里；展开后每次委派一行：标题是子任务名、目标是子 Agent 名、行尾「打开」。
  await page.getByRole("button", { name: /已处理/ }).click();
  const row = page.locator('[data-tool-call-id="delegate-ref-500"]');
  await expect(row).toContainText("审查代码");
  await expect(row).toContainText("Reviewer");
  const openReviewer = page.getByRole("button", { name: "打开子 Agent 工作台：审查代码" });
  await openReviewer.click();

  await expect(page.getByRole("complementary", { name: "工作台" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /审查代码/ })).toBeVisible();
  await expect.poll(() => reviewerSubscriptions(page)).toBe(1);
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
  await expect.poll(() => reviewerSubscriptions(page)).toBe(2);
  expect(businessWrites).toEqual([]);
});

test("两个并行子 Agent 分别订阅，切换回标签页重新订阅并保留内容", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.clear());

  await page.goto("/tasks/42");
  await expect(page.getByText("子 Agent 已开始工作。", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /已处理/ }).click();
  const reviewer = page.getByRole("button", { name: "打开子 Agent 工作台：审查代码" });
  const tester = page.getByRole("button", { name: "打开子 Agent 工作台：检查测试" });
  await reviewer.click();
  await expect(page.getByRole("tab", { name: /审查代码/ })).toBeVisible();
  await expect.poll(() => reviewerSubscriptions(page)).toBe(1);

  await tester.click();
  await expect(page.getByRole("tab", { name: /检查测试/ })).toBeVisible();
  await expect.poll(() => testerSubscriptions(page)).toBe(1);
  await expect(page.getByText("resumed response", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: /审查代码/ }).click();
  await expect(page.getByText("resumed response", { exact: true })).toBeVisible();
  expect((await frontendLogs(page)).filter((entry) => entry.event === "react_recoverable_render_error")).toEqual([]);
  // 只有激活标签挂载阅读页：切回 501 时重新订阅一次，502 不受影响。
  await expect.poll(() => reviewerSubscriptions(page)).toBe(2);
  expect(await testerSubscriptions(page)).toBe(1);
});
