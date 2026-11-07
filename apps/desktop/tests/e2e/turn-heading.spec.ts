import { expect, test } from "@playwright/test";
import { API_ORIGIN, OUTPUT_DIR } from "./e2e-env";
import { seedState } from "./transport-fixtures";
import { activity, assistantMessage, outcome, toolCallPart, transportState, userMessage } from "../unit/transport-fixtures";
import type { TransportMessagePart } from "../../lib/assistant/contract";

const text = (value: string, finalReply = false): TransportMessagePart => ({ type: "text", text: value, status: "completed", finalReply });
const reply = "子 Agent 已按你的取消停下。";
const done = (parts: TransportMessagePart[], durationMs: number = 129000) => transportState({
  messages: [userMessage("u", 1, "试一下委派子 Agent"), assistantMessage("a", 1, parts, outcome("completed", { durationMs }))],
  activity: activity(1, "completed"),
});
test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 42, title: "头部样式验证" } });
});

for (const theme of ["light", "dark"]) test(`${theme}: duration is the only left-aligned disclosure; keyboard toggles preserve reply DOM`, async ({ page, request }) => {
  await page.addInitScript(theme => localStorage.setItem("cosir.theme-preference", theme), theme);
  await seedState(request, 42, done([text("正在检查任务。"), toolCallPart("child", "read_file", { status: "cancelled" }), text(reply, true)]));
  await page.goto("/tasks/42");
  await expect(page.locator(".backend-startup-screen")).toHaveCount(0);
  const header = page.locator('[data-slot="turn-status"]');
  const button = header.getByRole("button");
  await expect(header).toHaveText("用时 2分钟9秒");
  await expect(button).toHaveAttribute("aria-expanded", "false");
  expect(Math.abs((await button.boundingBox())!.x - (await header.boundingBox())!.x)).toBeLessThan(1);
  expect((await button.boundingBox())!.width).toBeLessThan(220);
  const prose = page.getByText(reply, { exact: true });
  await prose.evaluate(el => { (window as unknown as { originalReply: Element }).originalReply = el; });
  await page.screenshot({ path: `${OUTPUT_DIR}/turn-heading-${theme}.png` });
  await button.focus();
  await page.keyboard.press("Enter");
  await expect(button).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("正在检查任务。", { exact: true })).toBeVisible();
  await page.keyboard.press("Space");
  await expect(button).toHaveAttribute("aria-expanded", "false");
  await expect(prose).toBeVisible();
  expect(await prose.evaluate(el => el === (window as unknown as { originalReply: Element }).originalReply)).toBe(true);
});

test("completed prose has no heading, divider or occupied space for any duration", async ({ page, request }) => {
  for (const duration of [129000, 0]) {
    await seedState(request, 42, done([text(reply, true)], duration));
    if (duration === 129000) await page.goto("/tasks/42");
    await expect(page.locator('[data-slot="turn-status"]')).toHaveCount(0);
    const prose = page.locator('[data-slot="prose-part"]');
    await expect(prose).toContainText(reply);
    const top = (await page.locator('[data-slot="turn"]').boundingBox())!.y;
    expect(Math.abs((await prose.boundingBox())!.y - top)).toBeLessThan(1);
    await expect(page.locator('[data-slot="turn"] button').first()).toBeVisible();
  }
});

test("quiet successful tools do not create a heading, but quiet failures remain discoverable", async ({ page, request }) => {
  const quiet = toolCallPart("quiet", "wait", { presentation: "quiet", status: "completed" });
  await seedState(request, 42, done([quiet, text(reply, true)]));
  await page.goto("/tasks/42");
  await expect(page.getByText(reply, { exact: true })).toBeVisible();
  await expect(page.locator('[data-slot="turn-status"]')).toHaveCount(0);
  await seedState(request, 42, done([{ ...quiet, status: "failed", error: "等待失败" }, text(reply, true)]));
  const button = page.locator('[data-slot="process-head"]');
  await expect(button).toContainText("有异常记录");
  await button.click();
  await expect(page.locator('[data-slot="activity-head"]')).toContainText("1 个失败");
});

test("terminal problems remain visible without tools", async ({ page, request }) => {
  await page.goto("/tasks/42");
  for (const [status, endReason, label] of [
    ["failed", null, "运行失败"], ["cancelled", null, "已停止"],
    ["cancelled", "runtime_restarted", "运行已中断 · 后端已重启"],
  ] as const) {
    const state = transportState({ messages: [userMessage("u", 1), assistantMessage("a", 1, [text("未完成回复")], outcome(status, { endReason }))], activity: activity(1, status) });
    await seedState(request, 42, state);
    await expect(page.locator('[data-slot="turn-status"]')).toHaveText(`${label} · 用时 0秒`);
    await expect(page.getByText("未完成回复", { exact: true })).toBeVisible();
  }
});

test("streaming prose preserves manual scrolling and settles at the bottom without remounting", async ({ page, request }) => {
  const body = Array.from({ length: 35 }, (_, i) => `第 ${i} 段：持续生成的回复。`).join("\n\n");
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { taskId: 42, hold: true,
    state: transportState({ messages: [userMessage("u", 1), assistantMessage("a", 1, [{ type: "text", text: body, status: "running" }])], activity: activity(1, "running", { startedAt: new Date().toISOString() }) }) } });
  await page.goto("/tasks/42");
  const prose = page.locator('[data-slot="prose-part"]');
  await expect(prose).toContainText("第 34 段");
  await expect(page.locator('[data-slot="turn-status"]')).toBeVisible();
  const original = await prose.elementHandle();
  const originalParagraph = await prose.locator("p").first().elementHandle();
  const viewport = page.locator(".aui-thread-root .overflow-y-auto").first();
  await viewport.evaluate(el => { el.scrollTop = 0; });
  await page.getByRole("button", { name: "回到底部", exact: true }).click();
  await expect.poll(() => viewport.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThan(3);
  const beforeAppend = await viewport.evaluate(el => el.scrollTop);
  const suffix = "\n\n追加的末尾内容。";
  await request.post(`${API_ORIGIN}/__test__/append-text`, { data: { taskId: 42, messageIndex: 1, partIndex: 0, text: suffix } });
  await expect(prose).toContainText("追加的末尾内容。");
  // 项目显式 autoScroll=false；追加不追底，仍由用户选择何时回到底部。
  await expect.poll(() => viewport.evaluate(el => el.scrollTop)).toBeCloseTo(beforeAppend, 0);
  await page.getByRole("button", { name: "回到底部", exact: true }).click();
  await expect.poll(() => viewport.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThan(3);
  await seedState(request, 42, done([text(body + suffix, true)], 3000));
  await expect(page.locator('[data-slot="turn-status"]')).toHaveCount(0);
  expect(await prose.evaluate((el, old) => el === old, original)).toBe(true);
  expect(await prose.locator("p").first().evaluate((el, old) => el === old, originalParagraph)).toBe(true);
  await expect.poll(() => viewport.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThan(3);
  const top = (await page.locator('[data-slot="turn"]').boundingBox())!.y;
  expect(Math.abs((await prose.boundingBox())!.y - top)).toBeLessThan(1);
});

test("zero duration remains explicit and tool failures stay visible", async ({ page, request }) => {
  await seedState(request, 42, done([toolCallPart("read", "read_file", { status: "failed", error: "读取失败" }), text(reply, true)], 0));
  await page.goto("/tasks/42");
  const button = page.locator('[data-slot="process-head"]');
  await expect(button).toHaveText("用时 0秒· 有异常记录");
  await button.click();
  await expect(page.locator('[data-slot="activity-head"]')).toContainText("1 个失败");
});

test("long live tool labels truncate within the narrow column without duplicating status", async ({ page, request }) => {
  await page.setViewportSize({ width: 768, height: 900 });
  const parts = [toolCallPart("cmd", "execute_terminal", { status: "running", intent: "LongAction".repeat(16), args: { command: "echo ok" } })];
  await seedState(request, 42, transportState({ messages: [userMessage("u", 1), assistantMessage("a", 1, parts)], activity: activity(1, "suspended") }));
  await page.goto("/tasks/42");
  await page.locator('[data-slot="activity-head"]').click();
  const row = page.locator('[data-tool-call-id="cmd"]');
  await expect(row).toContainText("LongAction".repeat(16));
  await expect(page.locator('[data-slot="current-activity"]')).toHaveCount(1);
  expect(await row.evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1);
});
