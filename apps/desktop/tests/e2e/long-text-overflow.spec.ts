import { expect, test } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { readState } from "./transport-fixtures";
import { activity, assistantMessage, outcome, toolCallPart, userMessage } from "../unit/transport-fixtures";

/**
 * 不能断开的长串（网址、无分隔的文件名）在共用的对话框与悬停提示里必须在框内折行。潜在缺陷：按内容定宽的
 * 容器被最小宽度撑过上限，文字与底栏伸出框外；调用方各自补 break-all，没补的地方照样溢出。
 */
const URL = "https://www.xiaohongshu.com/explore/69bb8ad900000000230222286?xsec_token=ABMJrpBLZ-stQqNN-0VIzPHoPwTRRxCGCIKAv5MmxKVVY=&xsec_source=pc_feed";
const LONG_NAME = `screenshot_${"x".repeat(90)}_final.png`;
const SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200"><rect width="320" height="200" fill="teal"/></svg>';

test("a confirm dialog wraps a long URL inside its own width", async ({ page }) => {
  await page.goto("/");
  await page.locator("main").waitFor();
  // 浏览器里经 Vite 按路径加载真实的确认入口（不经业务界面，只验证对话框外壳）。
  void page.evaluate(async ([url, modulePath]) => {
    const store = await import(/* @vite-ignore */ modulePath) as typeof import("../../lib/confirm/confirm-store");
    void store.requestConfirmation({ title: "打开外部链接？", description: url, confirmLabel: "打开" });
  }, [URL, "/lib/confirm/confirm-store.ts"] as const);
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("xsec_source");
  const widths = await dialog.evaluate((el) => {
    const box = el.getBoundingClientRect();
    const footer = el.querySelector('[data-slot="dialog-footer"]')!.getBoundingClientRect();
    const description = el.querySelector('[data-slot="dialog-description"]')!.getBoundingClientRect();
    return { box: box.width, footer: footer.width, descriptionRight: description.right - box.left };
  });
  expect(widths.footer).toBeLessThanOrEqual(widths.box + 1);
  expect(widths.descriptionRight).toBeLessThanOrEqual(widths.box);
});

test("a tooltip wraps a long unbroken file name inside its max width", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-mermaid`);
  const base = await readState(request, 42);
  const read = toolCallPart("r", "read_image", { status: "completed", intent: "看截图", display_data: {
    kind: "image-read", images: [{ path: LONG_NAME, asset_id: "a".repeat(64), width: 320, height: 200 }], failures: [],
  } });
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { sessionId: 42, hold: true, state: {
    ...base, messages: [userMessage("u", 1, "看图"), assistantMessage("a", 1, [{ type: "text", text: "开始。" }, read, { type: "text", text: "看完了。" }], outcome("completed"))], activity: activity(1, "completed"),
  } } });
  await page.route("**/workspaces/*/attachments/*/content", (route) => route.fulfill({ contentType: "image/svg+xml", body: SVG }));
  await page.setViewportSize({ width: 1100, height: 800 });
  await page.goto("/sessions/42");
  await page.locator('[data-slot="activity-head"]').first().click();
  await page.locator('[data-tool-call-id="r"] [data-slot="call-row"]').click();
  await page.locator('[data-tool-call-id="r"] [data-slot="image-tile"]').first().hover();
  const tip = page.locator('[data-slot="tooltip-content"]');
  await expect(tip).toContainText("_final.png");
  const size = await tip.evaluate((el) => ({ width: el.getBoundingClientRect().width, scroll: el.scrollWidth, max: parseFloat(getComputedStyle(el).maxWidth) }));
  expect(size.width).toBeLessThanOrEqual(size.max + 1);
  expect(size.scroll).toBeLessThanOrEqual(size.width + 1);
});
