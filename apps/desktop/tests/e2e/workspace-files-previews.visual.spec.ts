import { expect, test, type Page } from "@playwright/test";

import { API_ORIGIN, REAL_BACKEND_ORIGIN } from "./e2e-env";
import { readFileSync } from "node:fs";
import path from "node:path";

import { installFakeDesktopHost } from "./fake-desktop-host";
import { WorkspaceFilesBackend } from "./workspace-files-backend";

/**
 * 阶段三预览类型的视觉基线（设计 §11.3）：表格、Mermaid、音频、视频与「看不了的文件」，深浅两套。
 * 单独一个文件、单独一个夹具工作区：样本目录不会出现在 `workspace-files.visual.spec.ts` 的目录树里。
 */
const backend = new WorkspaceFilesBackend();

test.beforeAll(async ({ request }) => {
  await backend.start(request);
  await request.post(`${REAL_BACKEND_ORIGIN}/__test__/files/preview-fixture`);
});
test.afterAll(async () => {
  await backend.stop();
});

/** 假宿主给出的系统缩略图：借交付物基线的图表样本，像一张真的文档缩略图。 */
const THUMBNAIL = `data:image/png;base64,${readFileSync(path.join(__dirname, "fixtures/deliverables/chart.png")).toString("base64")}`;

async function openPreview(page: Page, theme: "light" | "dark", name: string) {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.addInitScript((preference) => {
    localStorage.clear();
    localStorage.setItem("cosir:theme", JSON.stringify({ state: { preference }, version: 0 }));
  }, theme);
  await backend.route(page);
  await page.goto("/");
  await page.getByRole("treeitem", { name: "demo", exact: true }).hover();
  await page.getByRole("button", { name: "在 demo 中新建会话" }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await surface.getByRole("button", { name: "展开目录 previews", exact: true }).click();
  await surface.getByRole("treeitem", { name, exact: true }).click();
  return { surface, panel: page.getByRole("complementary", { name: "文件", exact: true }) };
}

for (const theme of ["light", "dark"] as const) {
  test.describe(theme, () => {
    test.beforeEach(async ({ request }) => {
      await request.post(`${API_ORIGIN}/__test__/reset`);
    });

    test("表格：吸顶表头、数字右对齐、行号", async ({ page }) => {
      const { surface, panel } = await openPreview(page, theme, "cities.csv");
      await expect(panel).toHaveCSS("corner-shape", "superellipse(1.7)");
      await surface.getByRole("table", { name: "表格" }).getByRole("columnheader", { name: "人口" }).click();
      const table = surface.getByRole("table", { name: "表格" });
      await expect(table).toHaveCSS("corner-shape", "superellipse(1.5)");
      await expect(table.getByRole("row").first()).toHaveCSS("corner-top-left-shape", "superellipse(1.5)");
      await expect(table.getByRole("row").first()).toHaveCSS("corner-bottom-left-shape", /^(round|superellipse\(1\))$/);
      await expect(table).toHaveCSS("overflow", "visible");
      await expect(panel).toHaveScreenshot(`table-${theme}.png`);
    });

    test("Mermaid 渲染", async ({ page }) => {
      const { surface, panel } = await openPreview(page, theme, "flow.mmd");
      await expect(surface.locator(".aui-md svg[aria-roledescription]").first()).toBeVisible({ timeout: 15_000 });
      await expect(panel).toHaveScreenshot(`mermaid-${theme}.png`);
    });

    test("Mermaid 语法错误", async ({ page }) => {
      const { surface, panel } = await openPreview(page, theme, "broken.mmd");
      await expect(surface.getByText(/Mermaid Error/)).toBeVisible({ timeout: 15_000 });
      await expect(panel).toHaveScreenshot(`mermaid-error-${theme}.png`);
    });

    test("音频与视频", async ({ page }) => {
      const { surface, panel } = await openPreview(page, theme, "tone.wav");
      await expect.poll(() => surface.locator("audio").evaluate((element: HTMLAudioElement) => element.readyState)).toBeGreaterThanOrEqual(1);
      await expect(panel).toHaveScreenshot(`audio-${theme}.png`);
      const video = await openPreview(page, theme, "clip.webm");
      await expect.poll(() => video.surface.locator("video").evaluate((element: HTMLVideoElement) => element.readyState)).toBeGreaterThanOrEqual(2);
      await expect(video.panel).toHaveScreenshot(`video-${theme}.png`);
    });

    test("看不了的文件：系统认识的格式（macOS，有缩略图与快速查看）", async ({ page }) => {
      await installFakeDesktopHost(page, { platform: "darwin", thumbnail: THUMBNAIL });
      const { surface, panel } = await openPreview(page, theme, "report.docx");
      await expect(surface.locator('[data-slot="system-thumbnail"]')).toBeVisible();
      await expect(panel).toHaveScreenshot(`unviewable-system-${theme}.png`);
    });

    test("看不了的文件：系统不认识的格式", async ({ page }) => {
      await installFakeDesktopHost(page, { platform: "darwin" });
      const { surface, panel } = await openPreview(page, theme, "archive.zip");
      await expect(surface.locator('[data-slot="binary-preview"]')).toContainText("这个文件不是文本");
      await expect(panel).toHaveScreenshot(`unviewable-binary-${theme}.png`);
    });

    test("看不了的文件", async ({ page }) => {
      const { surface, panel } = await openPreview(page, theme, "fake.mp4");
      await expect(surface.locator('[data-slot="binary-preview"]')).toBeVisible();
      await expect(panel).toHaveScreenshot(`unviewable-${theme}.png`);
    });
  });
}
