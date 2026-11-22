import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";

import { desktopFetch, backendBaseUrl, launchDesktop, openNewConversation, unstartedSession } from "./desktop-app";

/** 真实 Electron + 后端，文件选择只替代系统对话框，验证 app:// 页面中的图片呈现与键盘焦点。 */
test("待发图片在真实桌面中完整显示、可预览和移除", async () => {
  const { app, page, home } = await launchDesktop();
  try {
    await expect(page.getByRole("navigation", { name: "工作区会话列表" })).toBeVisible({ timeout: 120_000 });
    const base = await backendBaseUrl(page);
    const root = path.join(home, "workspace");
    fs.mkdirSync(root);
    const response = await desktopFetch(`${base}/workspaces`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "图片交互验收", root_path: root }),
    });
    expect(response.status).toBe(200);
    const workspace = await response.json() as { workspace_id: number };
    const session = await unstartedSession(base, workspace.workspace_id);
    await page.reload();
    await openNewConversation(page, "图片交互验收", session);
    const input = page.getByLabel("消息输入");
    await expect(input).toBeEditable();
    await page.getByRole("button", { name: "添加", exact: true }).click();
    const choosing = page.waitForEvent("filechooser");
    await page.getByRole("option", { name: /^文件或图片/ }).click();
    await (await choosing).setFiles(["photo.png", "long.png"].map(name => path.resolve(__dirname, "../e2e/fixtures/deliverables", name)));
    const cards = page.locator('[data-slot="composer-image-card"]');
    await expect(cards).toHaveCount(2);
    await expect(cards.first().locator("img")).toHaveCSS("object-fit", "contain");
    expect(await cards.first().evaluate(element => [element.clientWidth, element.clientHeight])).toEqual([112, 112]);
    const first = page.getByRole("button", { name: "预览图片 photo.png" });
    await first.click();
    await expect(page.getByRole("dialog", { name: "放大的图片" })).toContainText("1 / 2");
    await page.keyboard.press("Escape");
    await expect(first).toBeFocused();
    await input.focus();
    await page.mouse.move(0, 0);
    await expect(page.getByRole("button", { name: "移除附件 photo.png" })).toHaveCSS("opacity", "0");
    await expect(page.getByRole("tooltip")).toBeHidden();
    await page.screenshot({ path: test.info().outputPath("composer-images-electron.png") });
    await first.focus();
    await page.keyboard.press("Tab");
    await page.keyboard.press("Space");
    await expect(page.getByRole("button", { name: "预览图片 long.png" })).toBeFocused();
    await page.keyboard.press("Tab");
    await page.keyboard.press("Space");
    await expect(cards).toHaveCount(0);
    await expect(input).toBeFocused();
  } finally {
    await app.close();
  }
});
