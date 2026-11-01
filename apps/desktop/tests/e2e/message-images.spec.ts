import type { TransportState } from "../../lib/assistant/contract";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { seedState } from "./transport-fixtures";
import { API_ORIGIN as api } from "./e2e-env";


const WIDE_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200"><rect width="300" height="200" fill="teal"/></svg>';

/** 把种子对话里用户消息的内容换成 `count` 张图片，图片内容一律返回同一张 SVG。 */
async function openWithUserImages(page: Page, request: APIRequestContext, count: number, svg = WIDE_SVG) {
  await page.route("**/workspaces/*/attachments/*/content", (route) => route.fulfill({
    contentType: "image/svg+xml",
    body: svg,
  }));
  await request.post(`${api}/__test__/seed-mermaid`);
  const snapshot = await (await request.get(`${api}/tasks/42/assistant/state`)).json() as TransportState;
  snapshot.messages.find((message) => message.role === "user")!.parts = Array.from({ length: count }, (_, index) => ({
    type: "image" as const, image: `cosir-attachment://${index.toString(16).padStart(64, "0")}`,
  }));
  await seedState(request, 42, snapshot);
  await page.goto("/tasks/42");
}

test.beforeEach(async ({ request }) => {
  await request.post(`${api}/__test__/reset`);
});

test("阈值以内的图片逐张铺开，点开后可在整组里左右翻看", async ({ page, request }) => {
  await openWithUserImages(page, request, 3);
  const tiles = page.getByRole("button", { name: /^预览图片 图片 \d+$/ });
  await expect(tiles).toHaveCount(3);
  await expect(page.locator('[data-slot="image-stack"]')).toHaveCount(0);

  await tiles.nth(1).click();
  const lightbox = page.getByRole("dialog", { name: "放大的图片" });
  await expect(lightbox).toContainText("2 / 3");
  await page.keyboard.press("ArrowRight");
  await expect(lightbox).toContainText("3 / 3");
  await expect(lightbox.getByRole("button", { name: "下一张" })).toBeDisabled();
  await lightbox.getByRole("button", { name: "上一张" }).click();
  await expect(lightbox).toContainText("2 / 3");
  await page.keyboard.press("Escape");
  await expect(lightbox).toBeHidden();
  await expect(tiles.nth(1)).toBeFocused();
});

// 目的：多图消息不撑出一面图墙，也不必逐张点开；潜在缺陷：超过阈值仍逐张铺开，或叠起后只能看到第一张。
test("超过 5 张收成一叠：点叠图从第一张翻看，展开后全部铺开、可再收起", async ({ page, request }) => {
  await openWithUserImages(page, request, 6);
  const stack = page.locator('[data-slot="image-stack"]');
  const tiles = page.getByRole("button", { name: /^预览图片 图片 \d+$/ });
  await expect(stack).toHaveAttribute("data-expanded", "false");
  await expect(tiles).toHaveCount(1);

  await tiles.first().click();
  const lightbox = page.getByRole("dialog", { name: "放大的图片" });
  await expect(lightbox).toContainText("1 / 6");
  await page.keyboard.press("ArrowRight");
  await expect(lightbox).toContainText("2 / 6");
  await page.keyboard.press("Escape");

  await stack.getByRole("button", { name: "展开 6" }).click();
  await expect(tiles).toHaveCount(6);
  await tiles.nth(4).click();
  await expect(lightbox).toContainText("5 / 6");
  await page.keyboard.press("Escape");
  await stack.getByRole("button", { name: "收起" }).click();
  await expect(tiles).toHaveCount(1);
});

// 目的：整页截图这类长图在缩略图和放大预览里都看得清；潜在缺陷：完整缩进小框或一屏，只剩一条细线。
test("长图缩略图裁切保留顶部，放大后按宽度铺满并可上下滚动", async ({ page, request }) => {
  const tall = '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="6000"><rect width="600" height="6000" fill="teal"/></svg>';
  await openWithUserImages(page, request, 1, tall);
  const tile = page.locator('[data-slot="image-tile"]');
  expect(await tile.evaluate((element: HTMLElement) => [element.offsetWidth, element.offsetHeight])).toEqual([120, 96]);
  await expect(tile.locator("img")).toHaveCSS("object-fit", "cover");
  await expect(tile.locator("img")).toHaveCSS("object-position", "50% 0%");

  await page.getByRole("button", { name: "预览图片 图片 1" }).click();
  const scroller = page.getByRole("dialog", { name: "放大的图片" }).locator('[data-slot="lightbox-scroll"]');
  await expect(scroller).toBeVisible();
  const { scrollHeight, clientHeight, width } = await scroller.evaluate((element) => ({
    scrollHeight: element.scrollHeight,
    clientHeight: element.clientHeight,
    width: element.getBoundingClientRect().width,
  }));
  expect(scrollHeight).toBeGreaterThan(clientHeight * 2);
  expect(width).toBeGreaterThan(300);
});
