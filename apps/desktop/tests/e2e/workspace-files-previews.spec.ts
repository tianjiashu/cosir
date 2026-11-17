import { expect, test, type Page } from "@playwright/test";

import { API_ORIGIN, REAL_BACKEND_ORIGIN } from "./e2e-env";
import { fakeBrowserCalls, installFakeDesktopHost } from "./fake-desktop-host";
import { WorkspaceFilesBackend } from "./workspace-files-backend";

/**
 * 阶段三的预览类型（真实后端）：表格、Mermaid、音视频、TIFF、.ipynb 高亮，以及「显示不了」统一降级到
 * 「看不了的文件」。样本由夹具端点按需生成在 previews/ 下，不影响其他用例的目录树。
 */
const backend = new WorkspaceFilesBackend();

test.beforeAll(async ({ request }) => {
  await backend.start(request);
  await request.post(`${REAL_BACKEND_ORIGIN}/__test__/files/preview-fixture`);
});
test.afterAll(async () => {
  await backend.stop();
});
test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
});

async function openPreview(page: Page, name: string) {
  await backend.route(page);
  await page.goto("/");
  await page.getByRole("treeitem", { name: "demo", exact: true }).hover();
  await page.getByRole("button", { name: "在 demo 中新建会话" }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await surface.getByRole("button", { name: "展开目录 previews", exact: true }).click();
  await surface.getByRole("treeitem", { name, exact: true }).click();
  return surface;
}

test("表格：排序三态、筛选、数字列右对齐、引号内换行，可切源码", async ({ page }) => {
  const surface = await openPreview(page, "cities.csv");
  const table = surface.getByRole("table", { name: "表格" });
  await expect(table).toBeVisible();
  await expect(surface.getByRole("status").filter({ hasText: "行" })).toHaveText("303 行");
  const population = table.getByRole("columnheader", { name: "人口" });
  const firstCity = () => table.getByRole("row").nth(1).getByRole("cell").nth(1);
  await population.click();
  await expect(population).toHaveAttribute("aria-sort", "ascending");
  await expect(firstCity()).toHaveText("城市1");
  await population.click();
  await expect(population).toHaveAttribute("aria-sort", "descending");
  await expect(firstCity()).toHaveText("上海");
  await population.click();
  await expect(population).toHaveAttribute("aria-sort", "none");
  await expect(firstCity()).toHaveText("杭州");
  await expect(table.getByRole("row").nth(1).getByRole("cell").nth(2)).toHaveCSS("text-align", "right");

  await surface.getByRole("textbox", { name: "筛选行" }).fill("西南");
  await expect(surface.getByRole("status").filter({ hasText: "行" })).toHaveText("1 / 303 行");
  await expect(firstCity()).toHaveText("成都");
  // 原始记录序号在筛选后不变。
  await expect(table.getByRole("row").nth(1).getByRole("cell").first()).toHaveText("3");

  await surface.getByRole("button", { name: "源码" }).click();
  await expect(surface.locator(".cm-content")).toContainText("城市,人口,备注");
});

// 潜在缺陷：表格视图按 Ctrl/Cmd+F 弹出 DOM 查找条（查不到虚拟滚动外的行，报「无结果」）；切到源码后查找丢失。
test("表格里的查找就是筛选行：Ctrl/Cmd+F 把焦点交给筛选框，源码视图仍是全文查找", async ({ page }) => {
  const surface = await openPreview(page, "cities.csv");
  await expect(surface.getByRole("table", { name: "表格" })).toBeVisible();
  await surface.getByRole("table", { name: "表格" }).click();
  await page.keyboard.press("ControlOrMeta+f");
  await expect(surface.getByRole("textbox", { name: "筛选行" })).toBeFocused();
  await expect(surface.getByRole("button", { name: "关闭搜索" })).toHaveCount(0);
  await page.keyboard.type("西南");
  await expect(surface.getByRole("status").filter({ hasText: "行" })).toHaveText("1 / 303 行");

  await surface.getByRole("button", { name: "源码" }).click();
  await expect(surface.locator(".cm-content")).toContainText("城市,人口,备注");
  await expect(surface.getByRole("button", { name: "查找文件内容" })).toBeVisible();
});

test("Mermaid：渲染成图", async ({ page }) => {
  const surface = await openPreview(page, "flow.mmd");
  // 认 mermaid 生成的图（带 aria-roledescription），不要匹配到加载中的转圈图标。
  await expect(surface.locator(".aui-md svg[aria-roledescription]").first()).toBeVisible({ timeout: 15_000 });
  await expect(surface.locator(".aui-md svg[aria-roledescription]").first()).toContainText("Start");
});

test("Mermaid 语法错误时显示错误，可切回源码", async ({ page }) => {
  const surface = await openPreview(page, "broken.mmd");
  await expect(surface.getByText(/Mermaid Error/)).toBeVisible({ timeout: 15_000 });
  await surface.getByRole("button", { name: "源码" }).click();
  await expect(surface.locator(".cm-content")).toContainText("A-->>>");
});

for (const name of ["tone.wav", "tone.webm"]) {
  test(`音频 ${name} 可以播放`, async ({ page }) => {
    const surface = await openPreview(page, name);
    // .webm 后缀分不出音视频，用 <video> 播，没有画面时换成音频版式。
    const media = surface.locator("audio, video");
    await expect.poll(() => media.evaluate((element: HTMLMediaElement) => element.readyState)).toBeGreaterThanOrEqual(1);
    await media.evaluate((element: HTMLMediaElement) => { element.muted = true; return element.play(); });
    await expect.poll(() => media.evaluate((element: HTMLMediaElement) => element.currentTime)).toBeGreaterThan(0);
    await expect(surface.locator('[data-slot="media-preview"]')).not.toHaveClass(/bg-black/);
  });
}

test("视频 webm 可以加载", async ({ page }) => {
  const surface = await openPreview(page, "clip.webm");
  const video = surface.locator("video");
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.videoWidth)).toBe(64);
  await expect(surface.locator('[data-slot="media-preview"]')).toHaveClass(/bg-black/);
});

test("不是视频的 .mp4 降级为看不了的文件", async ({ page }) => {
  const surface = await openPreview(page, "fake.mp4");
  const binary = surface.locator('[data-slot="binary-preview"]');
  await expect(binary).toContainText("此格式或编码无法在应用内播放");
  await expect(binary).toContainText("MP4 文件");
});

test("TIFF 转成 PNG 显示", async ({ page }) => {
  const surface = await openPreview(page, "scan.tif");
  const image = surface.getByRole("img", { name: "scan.tif", exact: true });
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBe(64);
});

test("损坏的图片在请求资源前就降级为看不了的文件", async ({ page }) => {
  const surface = await openPreview(page, "broken.png");
  await expect(surface.locator('[data-slot="binary-preview"]')).toContainText("图片已损坏或像素过大");
});

test("损坏的 PDF 降级为看不了的文件，仍可切源码看原文", async ({ page }) => {
  const surface = await openPreview(page, "broken.pdf");
  await expect(surface.locator('[data-slot="binary-preview"]')).toContainText("PDF 内容已损坏", { timeout: 15_000 });
  await surface.getByRole("button", { name: "源码" }).click();
  await expect(surface.locator(".cm-content")).toContainText("%PDF-1.7");
});

// 潜在缺陷：「显示不了」只是当前挂载的瞬时事实，源码偏好却会留下；面板重开后切换按钮消失，被困在乱码页。
test("降级后看过源码，面板收起再打开仍能切回预览，并重新降级", async ({ page }) => {
  const surface = await openPreview(page, "broken.pdf");
  const binary = surface.locator('[data-slot="binary-preview"]');
  await expect(binary).toContainText("PDF 内容已损坏", { timeout: 15_000 });
  await binary.getByRole("button", { name: "以文本方式查看" }).click();
  await expect(surface.locator(".cm-content")).toContainText("%PDF-1.7");
  await page.getByRole("button", { name: "收起文件", exact: true }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const reopened = page.getByTestId("workspace-files-surface");
  await expect(reopened.locator(".cm-content")).toContainText("%PDF-1.7");
  await reopened.getByRole("button", { name: "查看预览" }).click();
  await expect(reopened.locator('[data-slot="binary-preview"]')).toContainText("PDF 内容已损坏", { timeout: 15_000 });
});

test(".ipynb 按 JSON 高亮", async ({ page }) => {
  const surface = await openPreview(page, "notebook.ipynb");
  await expect(surface.locator(".cm-content")).toContainText('"nbformat"');
  // JSON 语言加载后属性名带高亮样式（不是纯文本的单一 span）。
  await expect.poll(() => surface.locator(".cm-line span").count()).toBeGreaterThan(0);
});

const THUMBNAIL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
const fileCalls = async (page: Page) => (await fakeBrowserCalls(page)).filter((call) => call.method.startsWith("files."));

// 目的：看不了的文件不再是死路；潜在缺陷：对系统不认识的格式也要缩略图（会拿到通用白纸图标），或平台没有的出口照样出现。
test("看不了的文件：系统认识的格式有缩略图与快速查看，出口交给宿主", async ({ page }) => {
  await installFakeDesktopHost(page, { platform: "darwin", thumbnail: THUMBNAIL });
  const surface = await openPreview(page, "report.docx");
  const binary = surface.locator('[data-slot="binary-preview"]');
  await expect(binary).toContainText("这种文件没有内置预览");
  await expect(binary.locator('[data-slot="system-thumbnail"]')).toHaveAttribute("src", THUMBNAIL);
  await expect(binary.getByRole("button")).toHaveText(["快速查看", "用系统应用打开", "在文件夹中显示", "以文本方式查看"]);
  await binary.getByRole("button", { name: "用系统应用打开" }).click();
  await expect.poll(() => fileCalls(page)).toEqual([
    { method: "files.thumbnail", args: [expect.stringMatching(/previews[\\/]report\.docx$/), 272] },
    { method: "files.openExternally", args: [expect.stringMatching(/previews[\\/]report\.docx$/)] },
  ]);
});

// 潜在缺陷：出口按格式过滤，空态与工具栏「更多」、胶囊菜单给的出口不一致。缩略图仍只对系统认识的格式取：
// 系统对不认识的格式返回一张通用白纸图标。
test("看不了的文件：系统不认识的格式不要缩略图，出口与其他文件一样", async ({ page }) => {
  await installFakeDesktopHost(page, { platform: "darwin", thumbnail: THUMBNAIL });
  const surface = await openPreview(page, "archive.zip");
  const binary = surface.locator('[data-slot="binary-preview"]');
  await expect(binary).toContainText("这个文件不是文本，无法预览");
  await expect(binary.getByRole("button")).toHaveText(["快速查看", "用系统应用打开", "在文件夹中显示", "以文本方式查看"]);
  expect(await fileCalls(page)).toEqual([]);
});

test("Linux 上没有快速查看与缩略图；没有宿主时只剩以文本方式查看", async ({ page }) => {
  await installFakeDesktopHost(page);
  let surface = await openPreview(page, "report.docx");
  await expect(surface.locator('[data-slot="binary-preview"]').getByRole("button")).toHaveText(["用系统应用打开", "在文件夹中显示", "以文本方式查看"]);
  expect(await fileCalls(page)).toEqual([]);
  const plain = await page.context().newPage();
  surface = await openPreview(plain, "report.docx");
  await expect(surface.locator('[data-slot="binary-preview"]').getByRole("button")).toHaveText(["以文本方式查看"]);
  await plain.close();
});

test("任何文件的「更多」里都有把文件交给系统的出口", async ({ page }) => {
  await installFakeDesktopHost(page, { platform: "darwin" });
  const surface = await openPreview(page, "cities.csv");
  await surface.getByRole("button", { name: "更多阅读选项" }).click();
  await page.getByRole("button", { name: "用系统应用打开" }).click();
  await expect.poll(() => fileCalls(page)).toEqual([{ method: "files.openExternally", args: [expect.stringMatching(/previews[\\/]cities\.csv$/)] }]);
});
