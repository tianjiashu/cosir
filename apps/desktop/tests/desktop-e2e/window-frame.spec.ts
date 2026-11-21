import { execFileSync } from "node:child_process";
import { expect, test } from "@playwright/test";

import { launchDesktop, waitForAppShell } from "./desktop-app";

/**
 * Windows 窗口外框：隐藏标题栏后系统窗口按钮叠在右上角（`titleBarOverlay`），页面按安全区让位。
 *
 * 能断言的只有页面一侧（标题栏形态与右侧安全区）。按钮本身由系统画在网页之外，`page.screenshot` 拍不到，
 * 所以按亮色、暗色各截一张屏幕上的窗口区域存进测试产物，按钮颜色是否随外观切换由人看。
 */
test.skip(process.platform !== "win32", "Windows titleBarOverlay only");

test("leaves room for the system window buttons and captures them in light and dark", async () => {
  const { app, page } = await launchDesktop();
  try {
    await waitForAppShell(page);

    const frame = await page.evaluate(() => ({
      titlebar: document.documentElement.dataset.titlebar,
      insetRight: getComputedStyle(document.documentElement).getPropertyValue("--titlebar-inset-right").trim(),
    }));
    expect(frame).toEqual({ titlebar: "windows-overlay", insetRight: "138px" });

    for (const theme of ["light", "dark"] as const) {
      // 像用户在外观设置里选一样改偏好（`lib/theme/theme-preference.ts` 的 persist 存储），页面与原生外观由应用一起同步；
      // 直接调宿主的 setThemeSource 会被「跟随系统」的页面改回去，两边对不上。
      await page.evaluate((preference) => {
        localStorage.setItem("cosir:theme", JSON.stringify({ state: { preference }, version: 0 }));
      }, theme);
      await page.reload();
      await waitForAppShell(page, 60_000);
      await expect.poll(() => page.evaluate(() => document.documentElement.classList.contains("dark"))).toBe(theme === "dark");
      await expect.poll(() => app.evaluate(({ nativeTheme }) => nativeTheme.shouldUseDarkColors)).toBe(theme === "dark");
      const bounds = await app.evaluate(({ BrowserWindow }) => {
        const window = BrowserWindow.getAllWindows()[0];
        window.show();
        window.focus();
        return window.getBounds();
      });
      // 让系统按新外观重画按钮。
      await page.waitForTimeout(1_000);
      captureScreen(bounds, test.info().outputPath(`window-frame-${theme}.png`));
    }
  } finally {
    await app.close();
  }
});

/** 用 GDI 从屏幕上截下窗口所在区域（含系统画的标题栏按钮）。 */
function captureScreen(bounds: { x: number; y: number; width: number; height: number }, file: string): void {
  const script = `
    Add-Type -AssemblyName System.Drawing
    $bitmap = New-Object System.Drawing.Bitmap ${bounds.width}, ${bounds.height}
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.CopyFromScreen(${bounds.x}, ${bounds.y}, 0, 0, $bitmap.Size)
    $bitmap.Save('${file.replace(/'/g, "''")}', [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose(); $bitmap.Dispose()
  `;
  execFileSync("powershell.exe", ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], { stdio: "inherit" });
}
