import { expect, test, type Page } from "@playwright/test";

import { completedState, seedState } from "./transport-fixtures";

const origin = "http://127.0.0.1:8000";
const TASK_ID = 62;

const rail = (page: Page) => page.getByRole("complementary", { name: "会话侧栏" });
const railButton = (page: Page) => page.getByRole("button", { name: /^会话侧栏/ });
const card = (page: Page) => page.getByRole("region", { name: "后台任务" });

test.beforeEach(async ({ page, request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: TASK_ID, title: "会话侧栏" } });
  await seedState(request, TASK_ID, completedState(1, { id: "user-1", parts: [{ type: "text", text: "跑一下构建" }] }, [
    { type: "tool-call", toolCallId: "call-bg", toolName: "terminal_read", args: {}, status: "pending", deferred: true },
  ]));
  await request.post(`${origin}/__test__/seed-background-tasks`, {
    data: {
      taskId: TASK_ID,
      entries: [{ toolCallId: "call-bg", kind: "terminal_command", label: "cd /work && make", status: "running", startedAt: new Date(Date.now() - 40_000).toISOString(), endedAt: null }],
      foregroundWaits: [],
    },
  });
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto(`/tasks/${TASK_ID}`);
  // 等种入的后台条目经 snapshot 到达（角标），之后的断言才不和首帧赛跑。
  await expect(page.getByRole("button", { name: /^会话侧栏，1 个后台任务运行中/ })).toBeVisible();
});

test("宽窗口固定为一列；打开工作台后浮起、不挤对话，关掉工作台恢复固定", async ({ page }, testInfo) => {
  await railButton(page).click();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "docked");
  await expect(card(page)).toContainText("1 个运行中");
  await expect(card(page)).toContainText("cd /work && make");
  await page.screenshot({ path: testInfo.outputPath("rail-docked.png") });

  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  await expect(page.getByRole("complementary", { name: "工作台" })).toBeVisible();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "floating");
  await page.screenshot({ path: testInfo.outputPath("rail-floating.png") });

  await page.getByRole("button", { name: "收起工作台", exact: true }).click();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "docked");
});

test("浮层非模态：点对话区不关闭，焦点在侧栏内按 Esc 关闭", async ({ page }) => {
  await page.setViewportSize({ width: 1100, height: 900 });
  await railButton(page).click();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "floating");
  // 浮层盖住的是对话区右上角，点左侧空白处的对话区。
  const thread = (await page.getByRole("main").boundingBox())!;
  await page.mouse.click(thread.x + 40, thread.y + thread.height / 2);
  await expect(rail(page)).toBeVisible();
  await card(page).getByRole("button", { name: "后台任务", exact: true }).focus();
  await page.keyboard.press("Escape");
  await expect(rail(page)).toHaveCount(0);
  await expect(railButton(page)).toHaveAttribute("aria-pressed", "false");
  // 焦点原本在已卸载的侧栏里，交还给页头按钮而不是落到 body。
  await expect(railButton(page)).toBeFocused();
});

test("开关与折叠是全局偏好，刷新后保留；折叠时摘要仍在", async ({ page }) => {
  await railButton(page).click();
  await card(page).getByRole("button", { name: "后台任务", exact: true }).click();
  await expect(card(page)).not.toContainText("cd /work && make");
  await expect(card(page)).toContainText("1 个运行中");
  await page.reload();
  await expect(rail(page)).toBeVisible();
  await expect(card(page)).toContainText("1 个运行中");
  await expect(card(page)).not.toContainText("cd /work && make");
});

test("只能按手柄拖动，键盘可拿起与取消；浮层里拖动中的 Esc 只取消拖动、不关闭侧栏", async ({ page }) => {
  await page.setViewportSize({ width: 1100, height: 900 });
  await railButton(page).click();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "floating");
  const handle = card(page).getByRole("button", { name: "拖动排序：后台任务" });
  await handle.focus();
  await page.keyboard.press("Space");
  await expect(handle).toHaveAttribute("aria-pressed", "true");
  // dnd-kit 的键盘传感器在拿起后的下一轮事件循环才挂上按键监听，人手不会快到这一步。
  await page.waitForTimeout(200);
  await page.keyboard.press("Escape");
  await expect(handle).not.toHaveAttribute("aria-pressed", "true");
  await expect(rail(page)).toBeVisible();
});

test("配置页隐藏侧栏与按钮，回到对话后恢复", async ({ page }) => {
  await railButton(page).click();
  await expect(rail(page)).toBeVisible();
  await page.getByRole("button", { name: "系统配置", exact: true }).click();
  await expect(page).toHaveURL(/\/settings/);
  await expect(rail(page)).toHaveCount(0);
  await expect(railButton(page)).toHaveCount(0);
  await page.goBack();
  await expect(rail(page)).toBeVisible();
});

test("快捷键切换会话侧栏", async ({ page }) => {
  const modifier = await page.evaluate(() => /mac/i.test(navigator.userAgent) ? "Meta" : "Control");
  await page.locator("body").click({ position: { x: 600, y: 400 } });
  await page.keyboard.press(`${modifier}+Alt+KeyR`);
  await expect(rail(page)).toBeVisible();
  await page.keyboard.press(`${modifier}+Alt+KeyR`);
  await expect(rail(page)).toHaveCount(0);
});
