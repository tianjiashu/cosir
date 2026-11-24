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
    await expect(first).toHaveCSS("corner-shape", "superellipse(1.5)");
    await expect(remove).toHaveCSS("corner-shape", /^(round|superellipse\(1\))$/);
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

type Upload = "fails" | "succeeds";
type Meanwhile = "stays" | "keeps-writing" | "switches-away";

/**
 * 点发送即整条交给 Session 级提交：输入框当场空出来、图片随乐观消息显示；上传在提交里完成。
 * 失败时整条内容回到原 Session 的输入框（并在用户写的新内容前面），成功时已发的图不会回到输入框、不会随下一条再发。
 */
for (const upload of ["fails", "succeeds"] as Upload[]) {
  for (const meanwhile of ["stays", "keeps-writing", "switches-away"] as Meanwhile[]) {
    test(`带图发送：上传${upload === "fails" ? "失败" : "成功"}，期间${{ stays: "停在原处", "keeps-writing": "接着写", "switches-away": "切走会话" }[meanwhile]}`, async ({ page, request }) => {
      await request.post(`${api}/__test__/seed-session`, { data: { sessionId: 101, title: "另一个会话" } });
      let release!: () => void;
      const gate = new Promise<void>((resolve) => { release = resolve; });
      const assetId = "a".repeat(64);
      await page.route(`${api}/workspaces/7/attachments`, async (route) => {
        await gate;
        await route.fulfill(upload === "succeeds"
          ? { json: { id: assetId, name: "原图.png", contentType: "image/png", byteSize: 68, width: 320, height: 200, locator: `cosir-attachment://${assetId}`, status: "ready" } }
          : { status: 503, json: { detail: { error: { code: "UPLOAD_FAILED", message: "测试上传失败", retryable: true } } } });
      });
      const posts: { type: string; text?: string; image?: string }[][] = [];
      page.on("request", (sent) => {
        if (sent.method() === "POST" && sent.url().endsWith("/sessions/100/messages")) posts.push(sent.postDataJSON().message.parts);
      });
      await page.goto("/sessions/100");
      const input = page.getByLabel("消息输入");
      const cards = page.locator('[data-slot="composer-image-card"]');
      await expect(page.getByRole("combobox", { name: "选择模型" })).toContainText("demo-model");
      await input.fill("待发送的原文");
      await pasteImages(input, [{ name: "原图.png" }]);
      await expect(cards).toHaveCount(1);
      const uploading = page.waitForRequest(`${api}/workspaces/7/attachments`);
      await page.getByRole("button", { name: "发送", exact: true }).click();
      await uploading;
      // 消息当场离开输入框：正文与图片都在乐观消息里。
      await expect(input).toHaveText("");
      await expect(cards).toHaveCount(0);
      await expect(page.locator('[data-role="user"]').last()).toContainText("待发送的原文");

      if (meanwhile === "keeps-writing") await input.fill("接着写的内容");
      if (meanwhile === "switches-away") {
        await page.getByRole("treeitem", { name: "另一个会话", exact: true }).click();
        await expect(page).toHaveURL(/sessions\/101$/);
      }
      release();
      if (upload === "fails") {
        if (meanwhile === "switches-away") await expect(page.getByText(/^发送失败，内容已放回「.+」的输入框$/)).toBeVisible();
        else await expect(page.getByRole("dialog")).toContainText("图片「原图.png」上传失败，消息未发出");
      } else {
        await expect.poll(() => posts.length).toBe(1);
        expect(posts[0]).toEqual([{ type: "text", text: "待发送的原文" }, { type: "image", image: `cosir-attachment://${assetId}` }]);
      }
      if (meanwhile === "switches-away") {
        await page.getByRole("treeitem", { name: "图片草稿", exact: true }).click();
        await expect(page).toHaveURL(/sessions\/100$/);
      } else if (upload === "fails") {
        await page.getByRole("dialog").getByRole("button").first().click();
      }

      if (upload === "fails") {
        expect(posts).toEqual([]);
        await expect(input).toContainText("待发送的原文");
        if (meanwhile === "keeps-writing") await expect(input).toContainText("接着写的内容");
        await expect(cards).toHaveCount(1);
        return;
      }
      await expect(cards).toHaveCount(0);
      if (meanwhile === "keeps-writing") await expect(input).toHaveText("接着写的内容");
      await input.fill("只想发文字");
      await page.getByRole("button", { name: "发送", exact: true }).click();
      await expect.poll(() => posts.length).toBe(2);
      expect(posts[1]).toEqual([{ type: "text", text: "只想发文字" }]);
    });
  }
}

/**
 * 提交顺序与乐观消息是会话级的：上传中切走再回来，带图那条仍显示在消息里，回来发的文字排在它后面——后端按到达
 * 顺序决定谁开 Run、谁排队，顺序颠倒等于模型先处理第二句。
 */
test("上传中切走再回来：带图的消息还在，回来发的文字等它发出后再发", async ({ page, request }) => {
  await request.post(`${api}/__test__/seed-session`, { data: { sessionId: 101, title: "另一个会话" } });
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const assetId = "c".repeat(64);
  await page.route(`${api}/workspaces/7/attachments`, async (route) => {
    await gate;
    await route.fulfill({ json: { id: assetId, name: "原图.png", contentType: "image/png", byteSize: 68, width: 320, height: 200, locator: `cosir-attachment://${assetId}`, status: "ready" } });
  });
  const posts: string[] = [];
  page.on("request", (sent) => {
    if (sent.method() === "POST" && sent.url().endsWith("/sessions/100/messages"))
      posts.push(sent.postDataJSON().message.parts.find((part: { type: string }) => part.type === "text").text);
  });
  await page.goto("/sessions/100");
  const input = page.getByLabel("消息输入");
  await expect(page.getByRole("combobox", { name: "选择模型" })).toContainText("demo-model");
  await input.fill("带图的第一条");
  await pasteImages(input, [{ name: "原图.png" }]);
  const uploading = page.waitForRequest(`${api}/workspaces/7/attachments`);
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await uploading;

  await page.getByRole("treeitem", { name: "另一个会话", exact: true }).click();
  await expect(page).toHaveURL(/sessions\/101$/);
  await page.getByRole("treeitem", { name: "图片草稿", exact: true }).click();
  await expect(page).toHaveURL(/sessions\/100$/);
  await expect(page.locator('[data-role="user"]', { hasText: "带图的第一条" })).toHaveCount(1);

  await expect(input).toBeEditable();
  await input.fill("回来后的第二条");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.locator('[data-role="user"]', { hasText: "回来后的第二条" })).toHaveCount(1);
  // 给第二条足够的机会抢跑：它要等第一条上传完、发出之后。
  await page.waitForTimeout(500);
  expect(posts).toEqual([]);
  release();
  await expect.poll(() => posts).toEqual(["带图的第一条", "回来后的第二条"]);
});

test("上传失败放回输入框的图片可以直接重发，重发时才上传", async ({ page }) => {
  let attempts = 0;
  const assetId = "b".repeat(64);
  await page.route(`${api}/workspaces/7/attachments`, async (route) => {
    attempts += 1;
    await route.fulfill(attempts === 1
      ? { status: 503, json: { detail: { error: { code: "UPLOAD_FAILED", message: "测试上传失败", retryable: true } } } }
      : { json: { id: assetId, name: "重发.png", contentType: "image/png", byteSize: 68, width: 320, height: 200, locator: `cosir-attachment://${assetId}`, status: "ready" } });
  });
  const posts: { type: string; image?: string }[][] = [];
  page.on("request", (sent) => {
    if (sent.method() === "POST" && sent.url().endsWith("/sessions/100/messages")) posts.push(sent.postDataJSON().message.parts);
  });
  await page.goto("/sessions/100");
  const input = page.getByLabel("消息输入");
  await expect(page.getByRole("combobox", { name: "选择模型" })).toContainText("demo-model");
  await pasteImages(input, [{ name: "重发.png" }]);
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("上传失败");
  await page.getByRole("dialog").getByRole("button").first().click();
  await expect(page.locator('[data-slot="composer-image-card"]')).toHaveCount(1);
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect.poll(() => posts).toEqual([[{ type: "image", image: `cosir-attachment://${assetId}` }]]);
  expect(attempts).toBe(2);
});
