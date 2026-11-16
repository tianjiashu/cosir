import { expect, test, type Page } from "@playwright/test";

import { API_ORIGIN } from "./e2e-env";
import { WorkspaceFilesBackend } from "./workspace-files-backend";

/**
 * 文件面板里图片的视觉基线：面板图片（适应、100%）与 Markdown 本地图片（在、不在），深浅两套。
 * 图片资源改为版本化地址直连时用它证明界面不变；规则见 `playwright.visual.config.ts`。
 */
const backend = new WorkspaceFilesBackend();

test.beforeAll(async ({ request }) => {
  await backend.start(request);
});
test.afterAll(async () => {
  await backend.stop();
});

async function openFile(page: Page, theme: "light" | "dark", segments: string[]) {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.addInitScript((preference) => {
    localStorage.clear();
    localStorage.setItem("cosir:theme", JSON.stringify({ state: { preference }, version: 0 }));
  }, theme);
  await backend.route(page);
  await page.goto("/");
  await page.getByRole("treeitem", { name: "demo", exact: true }).hover();
  await page.getByRole("button", { name: "在 demo 中新建会话" }).click();
  await expect(page).toHaveURL(/\/sessions\/\d+$/);
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  for (const directory of segments.slice(0, -1))
    await surface.getByRole("button", { name: `展开目录 ${directory}`, exact: true }).click();
  await surface.getByRole("treeitem", { name: segments.at(-1)!, exact: true }).click();
  // 测试 id 落在 `display: contents` 的容器上，没有盒子可截；截面板外框。
  return { surface, panel: page.getByRole("complementary", { name: "文件", exact: true }) };
}

async function loaded(page: Page, name: string) {
  const image = page.getByRole("img", { name, exact: true });
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.complete && element.naturalWidth > 0)).toBe(true);
  return image;
}

for (const theme of ["light", "dark"] as const) {
  test.describe(theme, () => {
    test.beforeEach(async ({ request }) => {
      await request.post(`${API_ORIGIN}/__test__/reset`);
    });

    test("面板图片：适应与 100%", async ({ page }) => {
      const { surface, panel } = await openFile(page, theme, ["media", "deep", "photo.png"]);
      await loaded(page, "photo.png");
      await expect(panel).toHaveScreenshot(`image-fit-${theme}.png`);
      await surface.getByRole("button", { name: "适应", exact: true }).click();
      await expect(surface.getByRole("button", { name: "100%", exact: true })).toBeVisible();
      await expect(panel).toHaveScreenshot(`image-actual-${theme}.png`);
    });

    test("Markdown 本地图片：在与不在", async ({ page }) => {
      const { surface, panel } = await openFile(page, theme, ["media", "gallery.md"]);
      await loaded(page, "photo");
      await expect(surface.getByText(/图片不存在：.*gone\.png/)).toBeVisible();
      await expect(panel).toHaveScreenshot(`markdown-images-${theme}.png`);
    });
  });
}
