import { expect, test } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import type { APIRequestContext } from "@playwright/test";
import type { TransportState } from "../../lib/assistant/contract";
const seedState = (request: APIRequestContext, sessionId: number, state: TransportState) => request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { sessionId, state, hold: true } });
import { activity, assistantMessage, outcome, toolCallPart, transportState, userMessage } from "../unit/transport-fixtures";
import type { TransportMessagePart, TransportOutcome } from "../../lib/assistant/contract";
const text = (value: string, finalReply = false): TransportMessagePart => ({ type: "text", text: value, finalReply });
const call = (id: string, standalone = false) => toolCallPart(id, standalone ? "fork_session" : "read_file", { presentation: standalone ? "standalone" : "activity", status: "completed" });
const state = (parts: TransportMessagePart[], end: TransportOutcome | null = null) => transportState({
  messages: [userMessage("u", 1, "时间线验收"), assistantMessage("a", 1, parts, end)],
  activity: activity(1, end?.status ?? "running", { endReason: end?.endReason ?? null, startedAt: new Date(Date.now() - 8000).toISOString() }),
});

test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-session`, { data: { sessionId: 42, title: "时间线验收" } });
});

for (const colorScheme of ["light", "dark"] as const) test(`A1–A3 ${colorScheme}: standalone folds with process; hover does not pin; final DOM and manual preference survive`, async ({ page, request }) => {
  await page.emulateMedia({ colorScheme });
  const parts = [text("调研进展"), call("read"), call("fork", true), text("最终结论")];
  await seedState(request, 42, state(parts));
  await page.goto("/sessions/42");
  const fork = page.locator('[data-slot="turn"] [data-tool-call-id="fork"]');
  await expect(fork).toBeVisible();
  expect(await fork.evaluate(el => !!el.closest('[data-slot="activity-group"]'))).toBe(false);
  await page.locator('[data-slot="activity-head"]').hover();
  const final = page.getByText("最终结论", { exact: true });
  await final.evaluate(el => { (window as unknown as { finalNode: Element }).finalNode = el; });
  const done = state([...parts.slice(0, -1), text("最终结论", true)], outcome("completed", { durationMs: 8000 }));
  await seedState(request, 42, done);
  const head = page.locator('[data-slot="process-head"]');
  await expect(head).toHaveAttribute("aria-expanded", "false");
  await expect(fork).toBeHidden();
  await expect(page.getByText("调研进展", { exact: true })).toBeHidden();
  expect(await final.evaluate(el => el === (window as unknown as { finalNode: Element }).finalNode)).toBe(true);
  await head.focus(); await page.keyboard.press("Enter");
  await expect(fork).toBeVisible();
  await seedState(request, 42, done);
  await expect(head).toHaveAttribute("aria-expanded", "true");
  await page.screenshot({ path: test.info().outputPath(`timeline-${colorScheme}.png`) });
  await page.setViewportSize({ width: 800, height: 720 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("A2 auto-collapse keeps the paragraph being read in place", async ({ page, request }) => {
  const process = Array.from({ length: 6 }, (_, i) => [text(`第 ${i} 步进展`), call(`read-${i}`)]).flat();
  const answer = Array.from({ length: 30 }, (_, i) => `第${i}段：${"用于确认阅读位置的回复正文。".repeat(8)}`).join("\n\n");
  await seedState(request, 42, state([...process, text(answer)]));
  await page.goto("/sessions/42");
  const paragraph = page.getByText("第14段").first();
  await expect(paragraph).toBeVisible();
  await page.evaluate(() => { const el = [...document.querySelectorAll("p")].find(p => p.textContent?.startsWith("第14段"))!; let viewport = el.parentElement!; while (getComputedStyle(viewport).overflowY !== "auto") viewport = viewport.parentElement!; viewport.scrollTop += el.getBoundingClientRect().top - 300; });
  await expect.poll(async () => Math.round((await paragraph.boundingBox())!.y)).toBe(300);
  await seedState(request, 42, state([...process, text(answer, true)], outcome("completed", { durationMs: 8000 })));
  await expect(page.locator('[data-slot="process-head"]')).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByText("第 0 步进展", { exact: true })).toBeHidden();
  await page.waitForTimeout(300);
  expect(Math.abs((await paragraph.boundingBox())!.y - 300)).toBeLessThan(2);
});

for (const [status, reason] of [["completed", "stop"], ["completed", "length"], ["cancelled", "runtime_restarted"], ["cancelled", "user_cancelled"], ["failed", "error"]] as const) test(`A4 ${status}/${reason}: no final remains open`, async ({ page, request }) => {
  await seedState(request, 42, state([text("已产生的进展"), call("fork", true)], outcome(status, { endReason: reason })));
  await page.goto("/sessions/42");
  await expect(page.locator('[data-slot="process-head"]')).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("已产生的进展", { exact: true })).toBeVisible();
  await expect(page.locator('[data-thinking-active="true"]')).toHaveCount(0);
});

test("A5/A10 running shimmer survives hidden process, is static for reduced motion and suspended", async ({ page, request }) => {
  const running = toolCallPart("read", "read_file", { status: "running" });
  await seedState(request, 42, state([running]));
  await page.goto("/sessions/42");
  await expect(page.locator('[data-slot="current-activity"] [data-thinking-active="true"]')).toBeVisible();
  await expect(page.locator('[data-slot="turn"] .animate-spin')).toHaveCount(0);
  await page.locator('[data-slot="process-head"]').click();
  const feedback = page.locator('[data-slot="current-activity"]');
  await expect(feedback).toContainText("读取文件");
  await page.emulateMedia({ reducedMotion: "reduce" });
  expect(await feedback.locator('span').evaluate(el => getComputedStyle(el).animationName)).toBe("none");
  const suspended = state([call("read")]); suspended.activity!.status = "suspended";
  await seedState(request, 42, suspended);
  await expect(feedback).toContainText("等待继续");
  await expect(feedback.locator('[data-thinking-active="true"]')).toHaveCount(0);
});

test("A10 automatic folding hands focus from the hidden process to its heading", async ({ page, request }) => {
  const parts = [call("read"), text("答复")];
  await seedState(request, 42, state(parts)); await page.goto("/sessions/42");
  await page.locator('[data-slot="activity-head"]').focus();
  await seedState(request, 42, state([parts[0], text("答复", true)], outcome("completed")));
  const head = page.locator('[data-slot="process-head"]');
  await expect(head).toHaveAttribute("aria-expanded", "false");
  await expect(head).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator('[data-slot="activity-head"]')).toBeVisible();
});

test("A10 a call awaiting the user stays visible in place, then folds with the process once decided", async ({ page, request }) => {
  const waiting = toolCallPart("hand", "browser_handoff", {
    presentation: "standalone", status: "pending", awaitingUser: true, args: { reason: "完成登录" },
    display_data: { kind: "browser-page", tab_id: "t1", url: "https://example.test/login", title: "登录", handoff_reason: "完成登录后交还" },
  });
  const final = text("结论", true);
  await seedState(request, 42, state([waiting, final], outcome("completed", { durationMs: 8000 })));
  await page.goto("/sessions/42");
  const head = page.locator('[data-slot="process-head"]');
  await expect(head).toHaveAttribute("aria-expanded", "false");
  // 过程收起，等你操作的调用仍在原位置，是一张卡片。
  const card = page.locator('[data-tool-call-id="hand"] [data-slot="awaiting-card"]');
  await expect(card).toBeVisible();
  // 焦点在卡片的按钮上时作出决定：卡片原地变回一行并随过程收起，卸载的按钮不拦着折叠。
  await card.getByRole("button", { name: "交还" }).focus();
  await seedState(request, 42, state([{ ...waiting, status: "completed", awaitingUser: false }, final], outcome("completed", { durationMs: 8000 })));
  await expect(card).toHaveCount(0);
  await expect(page.locator('[data-tool-call-id="hand"]')).toBeHidden();
  await head.click();
  await expect(page.locator('[data-tool-call-id="hand"] [data-slot="call-row"]')).toBeVisible();
});

test("A10 a focused control unmounting with its tool never blocks automatic folding", async ({ page, request }) => {
  const display = { kind: "terminal-result", command: "sleep 1", workdir: "/w", output: "" };
  const run = (status: "running" | "completed") => toolCallPart("term", "execute_terminal", { status, args: { command: "sleep 1" }, display_data: status === "completed" ? { ...display, end: "exited", exit_code: 0 } : display });
  const wait = { toolCallId: "term", kind: "terminal_command", label: "sleep", status: "running", startedAt: "2026-01-01T00:00:00Z", endedAt: null };
  await request.post(`${API_ORIGIN}/__test__/seed-background-tasks`, { data: { sessionId: 42, entries: [], foregroundWaits: [wait] } });
  await seedState(request, 42, state([text("进展"), run("running")]));
  await page.goto("/sessions/42");
  await page.locator('[data-slot="activity-head"]').click();
  // 去留操作在终端卡片底部状态条：先展开这条调用。
  await page.locator('[data-tool-call-id="term"] [data-slot="call-row"]').click();
  const button = page.getByRole("button", { name: "转到后台" });
  await button.focus();
  await request.post(`${API_ORIGIN}/__test__/seed-background-tasks`, { data: { sessionId: 42, entries: [], foregroundWaits: [] } });
  await seedState(request, 42, state([text("进展"), run("completed")]));
  await expect(button).toHaveCount(0);
  await seedState(request, 42, state([text("进展"), run("completed"), text("最终结论", true)], outcome("completed", { durationMs: 8000 })));
  await expect(page.locator('[data-slot="process-head"]')).toHaveAttribute("aria-expanded", "false");
});
