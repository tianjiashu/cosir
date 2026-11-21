import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import type { AddressInfo } from "node:net";
import { expect, test } from "@playwright/test";

import { backendBaseUrl, launchDesktop, openNewConversation, unstartedSession, waitForAppShell } from "./desktop-app";

/** 真实宿主与后端：DOM 裁切无法约束 WebContentsView，守住矩形槽的留白、原生 bounds 与冻结恢复。 */
test("浏览器原生画面退入圆角，铺满还原与冻结帧保持同一槽位", async () => {
  const testInfo = test.info();
  const server = http.createServer((_request, response) => {
    response.end('<title>圆角本机页面</title><body style="margin:0;background:#bfdbfe;box-shadow:inset 0 -8px #2563eb;min-height:100vh"><h1 style="margin:0">原生网页</h1><p>底部蓝线应完整留在面板圆角以内</p></body>');
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/`;
  try {
    const { app, page, home } = await launchDesktop();
    try {
      await waitForAppShell(page);
      const base = await backendBaseUrl(page);
      const root = path.join(home, "workspace");
      fs.mkdirSync(root);
      const response = await fetch(`${base}/workspaces`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "圆角验收", root_path: root }),
      });
      expect(response.status).toBe(200);
      const workspace = await response.json() as { workspace_id: number };
      const session = await unstartedSession(base, workspace.workspace_id);
      await page.reload();
      await openNewConversation(page, "圆角验收", session);
      await page.getByRole("button", { name: /^浏览器，/ }).click();
      const panel = page.getByRole("complementary", { name: "浏览器", exact: true });
      await panel.getByLabel("网址").fill(url);
      await panel.getByLabel("网址").press("Enter");
      await expect(panel.getByRole("tab", { name: "圆角本机页面" })).toBeVisible();
      // 模态弹层会把背后的面板标记为 aria-hidden，槽位仍在 DOM 中承担冻结画面。
      const slot = page.locator("[data-browser-slot]");
      const nativeViews = () => app.evaluate(({ BrowserWindow, WebContentsView }) => {
        const window = BrowserWindow.getAllWindows()[0];
        return window.contentView.children.flatMap(view => view instanceof WebContentsView && view.webContents !== window.webContents
          ? [{ id: view.webContents.id, url: view.webContents.getURL(), bounds: view.getBounds() }] : []);
      });
      const checkPlacement = async () => {
        await page.mouse.move(0, 0);
        await expect(page.getByRole("tooltip")).toBeHidden();
        await expect(panel).toHaveCSS("corner-shape", "superellipse(1.7)");
        await expect(panel.getByLabel("网址")).toHaveCSS("corner-shape", "superellipse(1.5)");
        const panelBox = (await panel.boundingBox())!;
        const slotBox = (await slot.boundingBox())!;
        const border = await panel.evaluate(element => parseFloat(getComputedStyle(element).borderBottomWidth));
        expect(panelBox.y + panelBox.height - border - slotBox.y - slotBox.height).toBeCloseTo(14, 1);
        const zoom = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getZoomFactor());
        const bounds = Object.fromEntries(Object.entries(slotBox).map(([key, value]) => [key, Math.round(value * zoom)]));
        await expect.poll(async () => (await nativeViews()).map(view => ({ url: view.url, bounds: view.bounds }))).toEqual([{ url, bounds }]);
        return panelBox.width;
      };
      const width = await checkPlacement();
      const id = (await nativeViews())[0].id;
      await page.screenshot({ path: testInfo.outputPath("browser-dom-normal.png") });
      await panel.getByRole("button", { name: "铺满浏览器面板" }).click();
      expect(await checkPlacement()).toBeGreaterThan(width);

      await panel.getByLabel("网址").click();
      await page.keyboard.press("ControlOrMeta+k");
      const frozen = slot.locator("img");
      await expect(frozen).toBeVisible();
      await expect.poll(() => frozen.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true);
      await expect.poll(nativeViews).toEqual([]);
      expect(await frozen.boundingBox()).toEqual(await slot.boundingBox());
      await page.screenshot({ path: testInfo.outputPath("browser-dom-frozen.png") });
      await page.keyboard.press("Escape");
      await expect(frozen).toHaveCount(0);
      await checkPlacement();
      expect((await nativeViews())[0].id).toBe(id);

      await panel.getByRole("button", { name: "还原浏览器面板" }).click();
      expect(await checkPlacement()).toBeCloseTo(width, 1);
      await testInfo.attach("native-view-restored", { body: JSON.stringify(await nativeViews()), contentType: "application/json" });
    } finally {
      await app.close();
    }
  } finally {
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  }
});
