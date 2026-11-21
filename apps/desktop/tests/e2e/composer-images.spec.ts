import { expect, test, type Locator } from "@playwright/test";
import { API_ORIGIN as api } from "./e2e-env";

/** 在真实剪贴板入口交付不同形状的 PNG，保留浏览器解码、对象 URL 与附件 runtime。 */
async function pasteImages(input: Locator, images: { name: string; width?: number; height?: number; broken?: boolean }[]) {
  await input.focus();
  await input.evaluate((element, images) => {
    const transfer = new DataTransfer();
    for (const image of images) {
      const canvas = document.createElement("canvas");
      canvas.width = image.width ?? 320;
      canvas.height = image.height ?? 200;
      const context = canvas.getContext("2d")!;
      context.fillStyle = "#90ae69";
      context.fillRect(10, 10, canvas.width - 20, canvas.height - 20);
      context.fillStyle = "#223322";
      context.font = "24px sans-serif";
      context.fillText(image.name, 20, 45);
      const bytes = image.broken ? new Uint8Array([0, 1, 2]) : Uint8Array.from(atob(canvas.toDataURL("image/png").split(",")[1]), (char) => char.charCodeAt(0));
      transfer.items.add(new File([bytes], image.name, { type: "image/png" }));
    }
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: transfer, bubbles: true, cancelable: true }));
  }, images);
}

test.beforeEach(async ({ page, request }) => {
  await request.post(`${api}/__test__/reset`);
  await request.post(`${api}/__test__/seed-session`, { data: { sessionId: 100, title: "图片草稿" } });
  await page.addInitScript(() => localStorage.clear());
});

for (const theme of ["light", "dark"]) {
  test(`${theme}：待发图完整展示，按需显示删除；灯箱翻页和焦点归还`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1200, height: 850 });
    await page.addInitScript((value) => localStorage.setItem("cosir:theme", JSON.stringify({ state: { preference: value }, version: 0 })), theme);
    await page.goto("/sessions/100");
    const input = page.getByLabel("消息输入");
    await expect(input).toBeEditable();
    await pasteImages(input, [
      { name: "横图.png", width: 600, height: 240 },
      { name: "竖图.png", width: 160, height: 320 },
      { name: "长图.png", width: 160, height: 1600 },
    ]);
    const cards = page.locator('[data-slot="composer-image-card"]');
    await expect(cards).toHaveCount(3);
    for (const card of await cards.all()) {
      expect(await card.evaluate(element => [element.clientWidth, element.clientHeight])).toEqual([112, 112]);
      await expect(card.locator("img")).toHaveCSS("object-fit", "contain");
    }
    const first = page.getByRole("button", { name: "预览图片 横图.png" });
    const remove = page.getByRole("button", { name: "移除附件 横图.png" });
    await page.mouse.move(0, 0);
    await expect(remove).toHaveCSS("opacity", "0");
    await first.hover();
    await expect(remove).toHaveCSS("opacity", "1");
    await first.click();
    const dialog = page.getByRole("dialog", { name: "放大的图片" });
    await expect(dialog).toContainText("1 / 3");
    await page.keyboard.press("ArrowRight");
    await expect(dialog).toContainText("2 / 3");
    await page.keyboard.press("ArrowRight");
    const longImage = dialog.locator('[data-slot="lightbox-scroll"]');
    await expect(longImage).toBeVisible();
    expect(await longImage.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);
    await page.keyboard.press("Escape");
    await expect(first).toBeFocused();
    await input.focus();
    await page.mouse.move(0, 0);
    await expect(remove).toHaveCSS("opacity", "0");
    await expect(page.getByRole("tooltip")).toBeHidden();
    await page.screenshot({ path: testInfo.outputPath(`composer-images-${theme}.png`) });
  });
}

test("键盘删除恢复相邻图片/正文焦点，移除后释放对象 URL，预览失败仍能删除", async ({ page }) => {
  await page.goto("/sessions/100");
  const input = page.getByLabel("消息输入");
  await pasteImages(input, [{ name: "一.png" }, { name: "二.png", broken: true }]);
  const first = page.getByRole("button", { name: "预览图片 一.png" });
  const second = page.getByRole("button", { name: "预览图片 二.png" });
  await expect(second).toContainText("无法预览");
  const objectUrl = (await first.locator("img").getAttribute("src"))!;
  await first.focus();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "移除附件 一.png" })).toBeFocused();
  await page.keyboard.press("Space");
  await expect(second).toBeFocused();
  await expect(page.getByRole("dialog", { name: "放大的图片" })).toHaveCount(0);
  await expect.poll(() => page.evaluate(url => fetch(url).then(() => true, () => false), objectUrl)).toBe(false);
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await expect(input).toBeFocused();
  await expect(page.locator('[data-slot="image-attachment-strip"]')).toBeHidden();
});

test("窄输入框保持单行，新增滚入可见范围且正文不丢焦点；无悬停设备可移除", async ({ page }) => {
  await page.setViewportSize({ width: 480, height: 760 });
  await page.goto("/sessions/100");
  const input = page.getByLabel("消息输入");
  await pasteImages(input, Array.from({ length: 20 }, (_, index) => ({ name: `${index}.png` })));
  const cards = page.locator('[data-slot="composer-image-card"]');
  await expect(cards).toHaveCount(20);
  await expect(cards.last()).toBeInViewport();
  await expect(input).toBeFocused();
  const strip = page.locator('[data-slot="image-attachment-strip"]');
  const geometry = await strip.evaluate(element => ({ height: element.clientHeight, width: element.clientWidth, scroll: element.scrollWidth, tops: [...element.children].map(child => child.getBoundingClientRect().top) }));
  expect(geometry.height).toBeGreaterThanOrEqual(112);
  expect(geometry.height).toBeLessThan(140);
  expect(geometry.scroll).toBeGreaterThan(geometry.width);
  expect(new Set(geometry.tops).size).toBe(1);
  // 媒体查询只替换设备能力，DOM 和交互仍由真实 Chromium 执行。
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 1 });
  await expect(cards.last().getByRole("button", { name: /^移除附件/ })).toHaveCSS("opacity", "1");
  await cdp.detach();
});

for (const text of ["保留正文", ""]) {
  test(`上传中移除图片不会被发送${text ? "，正文保留" : "，空消息不提交"}`, async ({ page }) => {
    await page.goto("/sessions/100");
    const input = page.getByLabel("消息输入");
    await pasteImages(input, [{ name: "上传中.png" }]);
    if (text) await input.fill(text);
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    const assetId = "a".repeat(64);
    await page.route(`${api}/workspaces/7/attachments`, async route => {
      await gate;
      await route.fulfill({ json: { id: assetId, name: "上传中.png", contentType: "image/png", byteSize: 68, width: 320, height: 200, locator: `cosir-attachment://${assetId}`, status: "ready" } });
    });
    const submitted: { type: string; text?: string; image?: string }[][] = [];
    await page.route(`${api}/sessions/100/messages`, async route => {
      submitted.push(route.request().postDataJSON().message.parts);
      await route.fulfill({ status: 400, json: { detail: { error: { code: "INVALID_MESSAGE", message: "测试拒绝提交", retryable: true } } } });
    });
    const upload = page.waitForRequest(`${api}/workspaces/7/attachments`);
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await upload;
    await page.getByRole("button", { name: "预览图片 上传中.png" }).hover();
    await page.getByRole("button", { name: "移除附件 上传中.png" }).click();
    await expect(page.locator('[data-slot="composer-image-card"]')).toHaveCount(0);
    release();
    if (text) {
      await expect(page.getByRole("dialog")).toContainText("测试拒绝提交");
      expect(submitted).toEqual([[{ type: "text", text }]]);
    } else {
      await input.fill("后续消息");
      await expect(page.getByRole("button", { name: "发送", exact: true })).toBeEnabled();
      expect(submitted).toEqual([]);
      await expect(input).toHaveText("后续消息");
    }
  });
}
