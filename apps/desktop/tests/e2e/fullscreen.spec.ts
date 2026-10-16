import type { TransportState } from "../../lib/assistant/contract";
import { expect, test, type Locator, type Page } from "@playwright/test";

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
  await request.post("http://127.0.0.1:8000/__test__/reset");
});

test("画布全屏覆盖保活任务布局，退出与弹窗保留编辑状态", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "全屏回归" } });
  await page.route("**/configuration/agent-teams", (route) => route.request().method() === "POST"
    ? route.fulfill({ status: 400, json: { detail: "测试保存失败" } })
    : route.fulfill({ json: [] }));
  await page.route("**/configuration/agents", (route) => route.fulfill({ json: [] }));
  await page.goto("/tasks/42");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await page.getByRole("button", { name: "系统配置", exact: true }).click();
  await page.getByRole("navigation", { name: "系统配置分类" }).getByRole("link", { name: "Agent Team", exact: true }).click();
  await page.getByRole("button", { name: "新建 Team" }).click();
  await page.getByRole("button", { name: "画布", exact: true }).click();
  const flowElement = await page.locator(".react-flow").elementHandle();
  await page.getByRole("button", { name: "Team 设置" }).click();
  await page.getByRole("dialog").getByLabel("Team 名称").fill("全屏草稿");
  await page.keyboard.press("Escape");
  // 人工加入定位包含块，验证全屏不依赖父页面当前恰好没有 transform。
  await page.locator("main").evaluate((element) => { element.style.transform = "translateZ(0)"; });
  await page.getByRole("button", { name: "全屏画布", exact: true }).click();
  const surface = page.getByRole("region", { name: "Agent Team 画布编辑器" });
  await expectViewportCoverage(page, surface);
  const exit = page.getByRole("button", { name: "退出全屏画布" });
  await expect(exit).toBeInViewport();
  expect(await flowElement!.evaluate((element) => element.isConnected)).toBe(true);
  await expect(surface.getByRole("heading", { name: "全屏草稿" })).toBeVisible();
  await expect(surface.getByRole("button", { name: "撤销", exact: true })).toBeEnabled();

  await page.getByRole("button", { name: "Team 设置" }).click();
  await page.getByRole("dialog").getByLabel("用途说明").fill("全屏下修改");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await expectViewportCoverage(page, surface);

  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByRole("dialog").getByText("测试保存失败")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await expectViewportCoverage(page, surface);

  await page.setViewportSize({ width: 1000, height: 680 });
  await expectViewportCoverage(page, surface);
  await expect(exit).toBeInViewport();
  await exit.focus();
  await exit.click();
  await expect(surface).not.toHaveClass(/fixed/);
  expect(await flowElement!.evaluate((element) => element.isConnected)).toBe(true);
  const enter = page.getByRole("button", { name: "全屏画布", exact: true });
  await expect(enter).toBeFocused();
  await enter.focus();
  await enter.press("Enter");
  await page.keyboard.press("Escape");
  await expect(surface).not.toHaveClass(/fixed/);
  await expect(enter).toBeFocused();
  await enter.click();
  await page.getByRole("button", { name: "返回 Team 编辑" }).click();
  await expect(surface).toBeHidden();
  await expect(page.getByLabel("用途说明")).toHaveValue("全屏下修改");
  expect(await page.evaluate(() => document.body.style.overflow)).toBe("");
  await page.getByRole("button", { name: "画布", exact: true }).click();
  expect(await flowElement!.evaluate((element) => element.isConnected)).toBe(true);
  await expect(surface.getByRole("button", { name: "撤销", exact: true })).toBeEnabled();
});

test("Mermaid 全屏避开消息裁剪，窗口缩放与两种退出入口可用", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-mermaid");
  await page.goto("/tasks/42");
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
  await request.post("http://127.0.0.1:8000/__test__/seed-mermaid");
  await page.route("**/tasks/42/assistant/state", async (route) => {
    const response = await route.fetch();
    const snapshot = await response.json() as TransportState;
    snapshot.runs[0].messages.find((message) => message.role === "user")!.parts = [{
      type: "image", image: `cosir-attachment://${"a".repeat(64)}`,
    }];
    await route.fulfill({ json: snapshot });
  });
  await page.goto("/tasks/42");
  const enter = page.getByRole("button", { name: "点击放大图片" });
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
