import { expect, test } from "@playwright/test";

import { API_ORIGIN } from "./e2e-env";

const openTooltip = (page: import("@playwright/test").Page) => page.locator('[data-slot="tooltip-content"][data-open]');

test.beforeEach(async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-tool-trace`);
  await page.goto("/sessions/42");
});

// 面板打开会把它的「收起文件」挪到静止的指针下面：提示要等指针真的停住才出现，不能立刻盖住正下方的「更多选项」。
test("布局变化挪到指针下的按钮不立刻弹提示，停住后才出现", async ({ page }) => {
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  await expect(page.getByRole("button", { name: "收起文件", exact: true })).toBeVisible();
  await page.waitForTimeout(250);
  await expect(openTooltip(page)).toHaveCount(0);
  await page.getByRole("button", { name: "文件浏览器更多选项" }).click();
  await expect(page.getByRole("menuitem", { name: "管理忽略项" })).toBeVisible();
});

// 全应用一个 Provider：一个提示出现后，滑到相邻按钮立即换成它的提示，扫一排图标不用每个都等。
test("提示停住才出现，出现后相邻按钮的提示立即切换", async ({ page }) => {
  await page.getByRole("button", { name: /^终端，/ }).hover();
  await expect(openTooltip(page)).toHaveCount(0);
  await expect(openTooltip(page)).toContainText("打开终端");
  await page.getByRole("button", { name: "浏览工作区文件" }).hover();
  await expect(openTooltip(page)).toContainText("浏览工作区文件", { timeout: 200 });
});
