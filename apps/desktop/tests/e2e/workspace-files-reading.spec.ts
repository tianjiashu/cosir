import { expect, test, type Page } from "@playwright/test";

import { API_ORIGIN } from "./e2e-env";
import { WorkspaceFilesBackend } from "./workspace-files-backend";
import { dragSelectElement } from "./text-selection-helpers";

/**
 * 阅读视图（Markdown 渲染）的定位与查找（真实后端）：样本由夹具 `reading-fixture` 按需生成在 reading/ 下，
 * 单独一个夹具工作区，不影响其他用例的目录树。
 */
const backend = new WorkspaceFilesBackend();

test.beforeAll(async () => {
  await backend.start();
  await backend.api.post("/__test__/files/reading-fixture");
});
test.afterAll(async () => {
  await backend.stop();
});
test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
});

async function openReadingFile(page: Page, name: string) {
  await backend.route(page);
  await page.goto("/");
  await page.getByRole("treeitem", { name: "demo", exact: true }).hover();
  await page.getByRole("button", { name: "在 demo 中新建会话" }).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  const surface = page.getByTestId("workspace-files-surface");
  await surface.getByRole("button", { name: "展开目录 reading", exact: true }).click();
  await surface.getByRole("treeitem", { name, exact: true }).click();
  return surface;
}

test("渲染视图查找：小写后变长的字符不让后面的命中越界或错位", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const surface = await openReadingFile(page, "turkish.md");
  await expect(surface.getByText("İstanbul – budget")).toBeVisible();
  await surface.getByRole("button", { name: "查找文件内容", exact: true }).click();
  await surface.getByLabel("在文件中查找").fill("budget");
  await expect(surface.getByText("1/1", { exact: true })).toBeVisible();
  const highlighted = await page.evaluate(() => [...(CSS.highlights.get("cosir-find-current") ?? [])].map((range) => range.toString()));
  expect(highlighted).toEqual(["budget"]);
  expect(errors).toEqual([]);
});

/** 真实鼠标选中一段 → 浮条「摘录」→ 回车：输入框上方多一条带来源的摘录。 */
async function quoteSelection(page: Page, surface: ReturnType<Page["getByTestId"]>, text: string) {
  const target = surface.getByText(text, { exact: true });
  await expect(target).toBeVisible();
  await dragSelectElement(page, target);
  await page.getByRole("button", { name: "摘录", exact: true }).click();
  await page.getByLabel("写批注").press("Enter");
  const bar = page.locator('[data-slot="composer-quotes"] [data-quote-bar]');
  await expect(bar).toHaveCount(1);
  return bar;
}

test("渲染视图选区摘录到对话：整篇读入的文件带源码行号", async ({ page }) => {
  const surface = await openReadingFile(page, "turkish.md");
  const bar = await quoteSelection(page, surface, "İstanbul – budget");
  await expect(bar).toContainText("turkish.md · L3");
  await expect(bar).toContainText("İstanbul – budget");
});

test("渲染视图选区摘录到对话：分段读取的后一段没有整篇行号，只带文件、不写段内行号", async ({ page }) => {
  const surface = await openReadingFile(page, "segmented.md");
  await surface.getByRole("button", { name: "下一段", exact: true }).click();
  const bar = await quoteSelection(page, surface, "SegmentMarker");
  await expect(bar).toContainText("segmented.md");
  await expect(bar).not.toContainText(/L\d/);
});
