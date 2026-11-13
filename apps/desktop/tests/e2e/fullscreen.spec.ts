import { expect, test, type Locator, type Page } from "@playwright/test";
import { readState, seedState } from "./transport-fixtures";
import { API_ORIGIN } from "./e2e-env";

async function expectViewportCoverage(page: Page, surface: Locator) {
  await expect.poll(() => surface.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  })).toEqual({ x: 0, y: 0, ...page.viewportSize()! });
  expect(await surface.evaluate((element) => [
    [5, 5], [innerWidth - 5, 5], [5, innerHeight - 5], [innerWidth - 5, innerHeight - 5],
  ].every(([x, y]) => element.contains(document.elementFromPoint(x, y))))).toBe(true);
}

test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
});

test("Mermaid 全屏避开消息裁剪，窗口缩放与两种退出入口可用", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/seed-mermaid`);
  await page.goto("/sessions/42");
  const diagram = page.locator('[data-streamdown="mermaid-block"]').last();
  await expect(diagram.locator('[aria-label="Mermaid chart"] > svg')).toBeVisible();
  const enter = diagram.getByRole("button", { name: "View fullscreen" });
  await enter.click();
  const surface = page.locator('[data-streamdown="mermaid-fullscreen"]');
  await expectViewportCoverage(page, surface);
  await expect(surface.locator('[aria-label="Mermaid chart"] > svg')).toBeVisible();
  await page.setViewportSize({ width: 1000, height: 680 });
  await expectViewportCoverage(page, surface);
  await surface.getByRole("button", { name: "Exit fullscreen" }).click();
  await expect(surface).toBeHidden();
  await enter.click();
  await page.keyboard.press("Escape");
  await expect(surface).toBeHidden();
  expect(await page.evaluate(() => document.body.style.overflow)).toBe("");
});

test("消息图片放大避开消息裁剪，关闭后恢复焦点与滚动", async ({ page, request }) => {
  await page.route("**/workspaces/*/attachments/*/content", (route) => route.fulfill({
    contentType: "image/svg+xml",
    body: '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200"><rect width="300" height="200" fill="teal"/></svg>',
  }));
  await request.post(`${API_ORIGIN}/__test__/seed-mermaid`);
  // 改写 seeded state 并交回模拟后端：state 读取与 attach 首帧共用同一份事实。
  const snapshot = await readState(request, 42);
  snapshot.messages.find((message) => message.role === "user")!.parts = [{
    type: "image", image: `cosir-attachment://${"a".repeat(64)}`,
  }];
  await seedState(request, 42, snapshot);
  await page.goto("/sessions/42");
  const enter = page.getByRole("button", { name: /^预览图片 图片 \d+$/ });
  await enter.click();
  const surface = page.getByRole("dialog", { name: "放大的图片" });
  await expectViewportCoverage(page, surface);
  await expect(surface.getByRole("img")).toBeVisible();
  await page.setViewportSize({ width: 1000, height: 680 });
  await expectViewportCoverage(page, surface);
  await surface.getByRole("button", { name: "关闭放大图片" }).click();
  await expect(surface).toBeHidden();
  await expect(enter).toBeFocused();
  await enter.press("Enter");
  await expect(surface).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(surface).toBeHidden();
  await expect(enter).toBeFocused();
  expect(await page.evaluate(() => document.body.style.overflow)).toBe("");
});
