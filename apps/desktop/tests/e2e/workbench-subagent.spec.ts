import { expect, test, type Page } from "@playwright/test";

import { frontendLogs, openedSubscriptionCount } from "./frontend-logs";
import { API_ORIGIN } from "./e2e-env";
import { completedState, seedState } from "./transport-fixtures";

test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-delegation`);
});

/** 子 Agent 只读阅读页已建立的订阅（501 审查代码、502 检查测试），口径见 `openedSubscriptionCount`。 */
const reviewerSubscriptions = (page: Page) => openedSubscriptionCount(page, { surface: "workbench", sessionId: 501 });
const testerSubscriptions = (page: Page) => openedSubscriptionCount(page, { surface: "workbench", sessionId: 502 });

test("只读子 Agent 的配置草稿可阅读但不能打开编辑器", async ({ page, request }) => {
  await seedState(request, 501, completedState(3, { id: "draft-input", parts: [{ type: "text", text: "准备配置" }] }, [
    {presentation: "activity",  type: "tool-call", toolCallId: "agent-draft", toolName: "propose_agent_configuration", args: {}, status: "completed", awaitingUser: false,
      display_data: { kind: "agent-configuration-draft", status: "draft", agent_id: "只读 Agent 草稿", role: "审查", description: "检查配置", system_prompt: "Review." } },
    { type: "text", text: "配置草稿已准备。" },
  ]));
  await page.goto("/sessions/42");
  await expect(page.locator('[data-tool-call-id="delegate-ref-500"]')).toBeVisible();
  await page.getByRole("button", { name: "打开子 Agent 工作台：审查代码" }).click();
  const reader = page.getByRole("complementary", { name: "工作台" });
  await expect(reader.locator('[data-slot="agent-configuration-draft-card"]')).toContainText("只读 Agent 草稿");
  await expect(reader.getByRole("button", { name: "编辑配置" })).toHaveCount(0);
});

test("主 Thread 委派后可在 Workbench 打开子 Agent 并接收只读流", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.clear());
  const businessWrites: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && (/\/sessions\/\d+\/(messages|resume)|\/tool-calls\/[^/]+\/decision/.test(request.url()) || request.url().includes("/cancel"))) businessWrites.push(request.url());
  });

  await page.goto("/sessions/42");
  await expect(page.getByText("子 Agent 已开始工作。", { exact: true })).toBeVisible();
  // 这里的委派没有后台记录，行上保留悬停才显示的「打开」；有后台记录时入口在后台卡片。
  await expect(page.locator('[data-tool-call-id="delegate-ref-500"]')).toBeVisible();
  const row = page.locator('[data-tool-call-id="delegate-ref-500"]');
  await expect(row).toContainText("审查代码");
  await expect(row).toContainText("delegate_reviewer");
  const openReviewer = page.getByRole("button", { name: "打开子 Agent 工作台：审查代码" });
  await openReviewer.click();

  await expect(page.getByRole("complementary", { name: "工作台" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /审查代码/ })).toBeVisible();
  await expect.poll(() => reviewerSubscriptions(page)).toBe(1);
  await expect(page.getByText("resumed response", { exact: true })).toBeVisible();
  expect((await frontendLogs(page)).filter((entry) => entry.event === "react_recoverable_render_error")).toEqual([]);
  const workbench = page.getByRole("complementary", { name: "工作台" });
  await expect(workbench.getByRole("button", { name: "发送", exact: true })).toHaveCount(0);
  await expect(workbench.getByRole("button", { name: "撤回" })).toHaveCount(0);
  await expect(workbench.getByRole("button", { name: "从这里分叉" })).toHaveCount(0);
  await expect(workbench.getByRole("button", { name: "关闭 审查代码" })).toBeVisible();
  expect(businessWrites).toEqual([]);
  await workbench.getByRole("button", { name: "关闭 审查代码" }).click();
  await expect(page.getByRole("complementary", { name: "工作台" })).toHaveCount(0);
  await openReviewer.click();
  await expect(page.getByRole("complementary", { name: "工作台" })).toBeVisible();
  await expect(page.getByText("resumed response", { exact: true })).toBeVisible();
  // 只读阅读页随标签挂载常驻订阅子 Session：关闭即释放，重新打开再订阅一次，不触发任何业务写入。
  await expect.poll(() => reviewerSubscriptions(page)).toBe(2);
  expect(businessWrites).toEqual([]);
});

test("两个并行子 Agent 分别订阅，切换回标签页重新订阅并保留内容", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.clear());

  await page.goto("/sessions/42");
  await expect(page.getByText("子 Agent 已开始工作。", { exact: true })).toBeVisible();
  await expect(page.locator('[data-tool-call-id="delegate-ref-500"]')).toBeVisible();
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
