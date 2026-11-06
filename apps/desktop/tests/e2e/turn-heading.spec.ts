import { expect, test } from "@playwright/test";
import { API_ORIGIN, OUTPUT_DIR } from "./e2e-env";
import { seedState } from "./transport-fixtures";
import { activity, assistantMessage, outcome, toolCallPart, transportState, userMessage } from "../unit/transport-fixtures";
import type { TransportMessagePart } from "../../lib/assistant/contract";

const text = (value: string): TransportMessagePart => ({ type: "text", text: value, status: "completed" });
const reply = "子 Agent 已按你的取消停下。";
const done = (parts: TransportMessagePart[], durationMs: number | null = 129000) => transportState({
  messages: [userMessage("u", 1, "试一下委派子 Agent"), assistantMessage("a", 1, parts, outcome("completed", { durationMs }))],
  activity: activity(1, "completed"),
});
test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 42, title: "头部样式验证" } });
});

for (const theme of ["light", "dark"]) test(`${theme}: duration is the only left-aligned disclosure; keyboard toggles preserve reply DOM`, async ({ page, request }) => {
  await page.addInitScript(theme => localStorage.setItem("cosir.theme-preference", theme), theme);
  await seedState(request, 42, done([text("正在检查任务。"), toolCallPart("child", "read_file", { status: "cancelled" }), text(reply)]));
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

test("no process means no fake button; no duration and no process means no heading", async ({ page, request }) => {
  await seedState(request, 42, done([text(reply)]));
  await page.goto("/tasks/42");
  const header = page.locator('[data-slot="turn-status"]');
  await expect(header).toHaveText("用时 2分钟9秒");
  await expect(header.getByRole("button")).toHaveCount(0);
  await seedState(request, 42, done([text(reply)], null));
  await expect(header).toHaveCount(0);
  await expect(page.getByText(reply, { exact: true })).toBeVisible();
});

test("unknown duration has a process fallback and failures remain visible beside it", async ({ page, request }) => {
  await seedState(request, 42, done([toolCallPart("read", "read_file", { status: "failed", error: "读取失败" }), text(reply)], null));
  await page.goto("/tasks/42");
  const button = page.locator('[data-slot="process-head"]');
  await expect(button).toHaveText("查看过程· 有异常记录");
  await expect(button).not.toContainText("0秒");
  await button.click();
  await expect(page.locator('[data-slot="activity-head"]')).toContainText("1 个失败");
});

test("long live tool labels truncate within the narrow column without duplicating status", async ({ page, request }) => {
  await page.setViewportSize({ width: 768, height: 900 });
  const parts = [toolCallPart("cmd", "execute_terminal", { status: "running", args: { description: "LongAction".repeat(40), command: "echo ok" } })];
  await seedState(request, 42, transportState({ messages: [userMessage("u", 1), assistantMessage("a", 1, parts)], activity: activity(1, "suspended") }));
  await page.goto("/tasks/42");
  const row = page.locator('[data-tool-call-id="cmd"]');
  await expect(row).toContainText("LongAction".repeat(40));
  await expect(page.locator('[data-slot="current-activity"]')).toHaveCount(0);
  expect(await row.evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(1);
});
