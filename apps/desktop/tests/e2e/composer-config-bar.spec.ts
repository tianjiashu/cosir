import { expect, test } from "@playwright/test";

import { completedState, seedState } from "./transport-fixtures";
import { API_ORIGIN as origin } from "./e2e-env";

/**
 * 输入框框外的配置行：输入框里只有「+」、正文与发送；模型、终端权限、上下文在框外。终端权限是带说明的
 * 菜单，切换经后端推回，完全访问权限时按钮用警示色。
 */

const SESSION_ID = 42;

test.beforeEach(async ({ page, request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId: SESSION_ID, title: "配置行" } });
  await seedState(request, SESSION_ID, completedState(1,
    { id: "u-1", parts: [{ type: "text", text: "你好", status: "completed" }] },
    [{ type: "text", text: "你好，有什么要做的？", status: "completed" }]));
  await page.addInitScript(() => window.localStorage.clear());
});

test("配置在框外，终端权限菜单带说明，切换后按钮随推送更新", async ({ page, request }) => {
  await page.goto(`/sessions/${SESSION_ID}`);
  const composer = page.locator("form").filter({ has: page.getByLabel("消息输入") });
  await expect(composer).toBeVisible();
  // 框里只装写东西的入口；配置与状态在框外。
  await expect(composer.getByRole("button", { name: "添加" })).toBeVisible();
  await expect(composer.getByLabel("选择模型")).toHaveCount(0);
  await expect(composer.getByTestId("session-context-usage")).toHaveCount(0);
  await expect(page.getByLabel("选择模型")).toBeVisible();
  await expect(page.getByTestId("session-context-usage")).toBeVisible();

  const trigger = page.getByRole("button", { name: "终端权限：请求批准" });
  await trigger.click();
  const menu = page.getByRole("listbox", { name: "终端权限" }).or(page.getByLabel("终端权限", { exact: true }));
  await expect(menu.getByText("沙箱内直接运行，越界的命令先问你")).toBeVisible();
  await expect(menu.getByText("命令不受沙箱限制，可联网、可写工作区外")).toBeVisible();
  await menu.getByText("完全访问权限", { exact: true }).click();

  const full = page.getByRole("button", { name: "终端权限：完全访问权限" });
  await expect(full).toBeVisible();
  await expect(full).toHaveClass(/text-warning/);
  const telemetry = await (await request.get(`${origin}/__test__/telemetry`)).json() as { requestBodies: Array<Record<string, unknown>> };
  expect(telemetry.requestBodies.filter((body) => body.terminalPermission)).toEqual([{ mode: "full_access", sessionId: SESSION_ID, terminalPermission: true }]);
});

test("终端权限菜单只用键盘就能切换：打开停在当前项，↓ 移动，回车提交", async ({ page, request }) => {
  await page.goto(`/sessions/${SESSION_ID}`);
  const trigger = page.getByRole("button", { name: "终端权限：请求批准" });
  await trigger.focus();
  await page.keyboard.press("Enter");
  const menu = page.locator("[cmdk-root]").filter({ has: page.getByText("完全访问权限", { exact: true }) });
  await expect(menu).toBeFocused();
  await expect(menu.locator('[aria-selected="true"]')).toContainText("请求批准");
  await page.keyboard.press("ArrowDown");
  await expect(menu.locator('[aria-selected="true"]')).toContainText("完全访问权限");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "终端权限：完全访问权限" })).toBeVisible();
  const telemetry = await (await request.get(`${origin}/__test__/telemetry`)).json() as { requestBodies: Array<Record<string, unknown>> };
  expect(telemetry.requestBodies.filter((body) => body.terminalPermission)).toEqual([{ mode: "full_access", sessionId: SESSION_ID, terminalPermission: true }]);
});

test("窄对话区里配置按钮不折行，只有长模型名截断", async ({ page }) => {
  await page.setViewportSize({ width: 960, height: 800 });
  await page.route("**/configuration/model-connections", async (route) => {
    const data = await (await route.fetch()).json();
    data.connections[0].models[0].model_name = "openai-compatible/deepseek-prover-v2-671b-instruct-2026-11";
    data.connections[0].models[0].supports_reasoning_effort = true;
    await route.fulfill({ json: data });
  });
  await page.goto(`/sessions/${SESSION_ID}`);
  // 打开文件面板让对话区变窄（窗口不变），配置行按输入框所在容器的宽度让位。
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  await page.getByRole("button", { name: "终端权限：请求批准" }).click();
  await page.getByRole("option", { name: /完全访问权限/ }).click();
  const full = page.getByRole("button", { name: "终端权限：完全访问权限" });
  await expect(full).toBeVisible();
  const lines = await full.evaluate((button) => {
    const range = document.createRange();
    range.selectNodeContents(button);
    return range.getClientRects().length;
  });
  expect(lines).toBe(1);
});
