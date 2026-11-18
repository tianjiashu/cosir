import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { readState } from "./transport-fixtures";
import { activity, assistantMessage, outcome, toolCallPart, userMessage } from "../unit/transport-fixtures";
import type { TransportMessagePart, TransportState } from "../../lib/assistant/contract";

/**
 * 活动段汇总行上浮产出：改动增删行数之和与最近 3 张图。潜在缺陷：窄屏折行或挤掉失败数、缩略图嵌进展开按钮、
 * 「+N」打不开更早的图、运行中不累加。
 */
const SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200"><rect width="320" height="200" fill="teal"/></svg>';
const asset = (i: number) => i.toString(16).padStart(64, "0");
const edit = (id: string, insertions: number, deletions: number) => toolCallPart(id, "edit_file", {
  status: "completed", display_data: { kind: "file-changes", changes: [{ path: `src/${id}.ts`, status: "modified", patch: null, insertions, deletions }] },
});
const shot = (id: string, i: number) => toolCallPart(id, "browser_screenshot", {
  status: "completed", display_data: { kind: "browser-page", tab_id: "t1", url: "https://example.com", title: `截图 ${i}`, screenshot: asset(i) },
});
const read = (id: string, from: number, count: number) => toolCallPart(id, "read_image", {
  status: "completed", display_data: { kind: "image-read", images: Array.from({ length: count }, (_, k) => ({ path: `img/${from + k}.png`, asset_id: asset(from + k), width: 320, height: 200 })), failures: [] },
});
const busy: TransportMessagePart[] = [
  { type: "text", text: "开始排查。" },
  edit("a", 30, 6), read("r", 1, 2), toolCallPart("s1", "search_content", { status: "completed" }),
  toolCallPart("f1", "find_files", { status: "failed" }), toolCallPart("t1", "execute_terminal", { status: "cancelled" }),
  shot("b", 3), edit("c", 4, 0), toolCallPart("w1", "web_fetch", { status: "completed" }), shot("d", 4), shot("e", 5),
  { type: "text", text: "排查告一段落。" },
];

let base: TransportState;
/** `hold`：运行中的种子不让模拟后端接着往下演，页面看到的就是写进去的这一份。 */
const seed = (request: APIRequestContext, parts: TransportMessagePart[], running: boolean) => request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { sessionId: 42, hold: true, state: {
  ...base, messages: [userMessage("u", 1, "排查"), assistantMessage("a", 1, parts, running ? null : outcome("completed"))],
  activity: activity(1, running ? "running" : "completed"),
} } });
async function open(page: Page, request: APIRequestContext, parts: TransportMessagePart[], running = false) {
  await seed(request, parts, running);
  await page.goto("/sessions/42");
}

test.beforeEach(async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-mermaid`);
  base = await readState(request, 42);
  await page.route("**/workspaces/*/attachments/*/content", (route) => route.fulfill({ contentType: "image/svg+xml", body: SVG }));
});

for (const width of [1100, 640, 420, 360]) test(`one line at ${width}px: problems always visible, images only when there is room`, async ({ page, request }) => {
  await page.setViewportSize({ width, height: 800 });
  await open(page, request, busy);
  await page.locator('[data-slot="activity-head"]').first().waitFor();
  const row = page.locator('[data-slot="activity-row"]').first();
  const head = row.locator('[data-slot="activity-head"]');
  expect((await head.boundingBox())!.height).toBeLessThanOrEqual(33);
  await expect(head).toContainText("+34");
  await expect(head).toContainText("−6");
  await expect(head).toContainText("1 个失败");
  await expect(head).toContainText("1 个取消");
  // 失败数没被挤出：它的右边缘在行内。
  const problem = head.getByText(/1 个失败/);
  const [p, r] = [(await problem.boundingBox())!, (await row.boundingBox())!];
  expect(p.x + p.width).toBeLessThanOrEqual(r.x + r.width + 1);
  // 缩略图不在按钮里。
  await expect(head.locator("button, [data-slot='image-tile']")).toHaveCount(0);
  const images = row.locator('[data-slot="activity-images"]');
  if (width >= 640) await expect(images.locator('[data-slot="image-tile"]')).toHaveCount(3);
  else await expect(images).toBeHidden();
  await page.screenshot({ path: test.info().outputPath(`row-${width}.png`), clip: { x: 0, y: r.y - 40, width, height: r.height + 80 }, animations: "disabled" });
});

test("thumbnails open the whole segment's images; +N starts from the first; the row still expands", async ({ page, request }) => {
  await page.setViewportSize({ width: 1100, height: 800 });
  await open(page, request, busy);
  const row = page.locator('[data-slot="activity-row"]').first();
  const lightbox = page.getByRole("dialog", { name: "放大的图片" });
  await row.getByRole("button", { name: "预览图片 截图 5" }).click();
  await expect(lightbox).toContainText("5 / 5");
  await page.keyboard.press("Escape");
  await row.getByRole("button", { name: "另有 2 张，从第一张开始查看" }).click();
  await expect(lightbox).toContainText("1 / 5");
  await page.keyboard.press("Escape");
  await expect(lightbox).toBeHidden();
  await row.locator('[data-slot="activity-head"]').click();
  await expect(row.locator('[data-slot="activity-head"]')).toHaveAttribute("aria-expanded", "true");
});

test("while running the row accumulates diff and images as calls land", async ({ page, request }) => {
  await page.setViewportSize({ width: 1100, height: 800 });
  const start: TransportMessagePart[] = [{ type: "text", text: "开始。" }, edit("a", 3, 1), toolCallPart("r", "read_image", { status: "running" })];
  await open(page, request, start, true);
  const row = page.locator('[data-slot="activity-row"]').first();
  await expect(row.locator('[data-slot="current-activity-label"]')).toHaveText("查看图片");
  await expect(row).toContainText("+3");
  await expect(row.locator('[data-slot="image-tile"]')).toHaveCount(0);
  await seed(request, [...start.slice(0, 2), read("r", 1, 1), edit("c", 2, 2)], true);
  await expect(row).toContainText("+5");
  await expect(row).toContainText("−3");
  await expect(row.locator('[data-slot="image-tile"]')).toHaveCount(1);
});

for (const colorScheme of ["light", "dark"] as const) test(`${colorScheme} appearance`, async ({ page, request }) => {
  await page.emulateMedia({ colorScheme });
  await page.setViewportSize({ width: 1100, height: 800 });
  await open(page, request, busy);
  const row = page.locator('[data-slot="activity-row"]').first();
  await expect(row.locator('[data-slot="image-tile"] img').first()).toBeVisible();
  const r = (await row.boundingBox())!;
  await page.screenshot({ path: test.info().outputPath(`row-${colorScheme}.png`), clip: { x: 0, y: r.y - 60, width: 1100, height: r.height + 120 }, animations: "disabled" });
});
