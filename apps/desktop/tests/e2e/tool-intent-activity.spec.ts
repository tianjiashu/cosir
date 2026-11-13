import { expect, test, type APIRequestContext } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { activity, assistantMessage, outcome, toolCallPart, transportState, userMessage } from "../unit/transport-fixtures";
import type { TransportState, TransportMessagePart } from "../../lib/assistant/contract";

const seed = async (request: APIRequestContext, state: TransportState) => {
  await request.post(`${API_ORIGIN}/__test__/seed-background-tasks`, { data: { sessionId: 42, entries: state.background_tasks } });
  return request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { sessionId: 42, state, hold: true } });
};
const model = (parts: TransportMessagePart[], phase: "reasoning" | "responding" | null = null) => transportState({ messages: [userMessage("u", 1, "统一意图验收"), assistantMessage("a", 1, parts)], activity: activity(1, "running", { modelActivity: phase ? { stepId: "s", phase } : null }) });
test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-session`, { data: { sessionId: 42, title: "统一意图验收" } });
});

for (const colorScheme of ["light", "dark"] as const) test(`I6–I9 ${colorScheme}: single reading-position activity, real folding and stable prose`, async ({ page, request }) => {
  await page.emulateMedia({ colorScheme });
  const initial = model([], "reasoning"); initial.messages.pop();
  await seed(request, initial); await page.goto("/sessions/42");
  const feedback = page.locator('[data-slot="current-activity"]');
  await expect(feedback).toHaveCount(1); await expect(feedback).toHaveText("正在思考");
  const prose = { type: "text" as const, text: "先检查文件配置。" };
  await seed(request, model([prose], "reasoning"));
  await expect(feedback).toHaveCount(1);
  const text = page.getByText(prose.text, { exact: true });
  await text.evaluate(el => Object.assign(window, { intentProse: el }));
  const call = toolCallPart("read", "read_file", { status: "running", intent: "核对身份验证配置", args: { path: "config.ts" } });
  await seed(request, model([prose, call]));
  await expect(feedback).toHaveText(call.intent!);
  await expect(page.locator('[data-tool-call-id="read"]')).toHaveCount(0);
  const head = page.locator('[data-slot="activity-head"]');
  await head.click();
  await expect(page.locator('[data-tool-call-id="read"]')).toBeVisible();
  await seed(request, model([prose, { ...call, status: "completed" }], "reasoning"));
  await expect(feedback).toHaveText("正在思考");
  await expect(head).toHaveAttribute("aria-expanded", "true");
  await head.click();
  await expect(page.locator('[data-tool-call-id="read"]')).toBeHidden();
  const more = { type: "text" as const, text: "已定位配置，继续检查。" };
  await seed(request, model([prose, { ...call, status: "completed" }, more], "reasoning"));
  await expect(feedback).toHaveCount(1);
  await expect(head).toHaveText("读取文件 1 次");
  expect(await text.evaluate(el => el === (window as unknown as { intentProse: Element }).intentProse)).toBe(true);
  const long = toolCallPart("long", "read_file", { status: "running", intent: "Review".repeat(26), args: { path: "long.ts" } });
  await seed(request, model([prose, { ...call, status: "completed" }, more, long]));
  await page.setViewportSize({ width: 760, height: 720 });
  await expect(feedback).toHaveText(long.intent!);
  expect(await feedback.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  const footer = page.locator('[data-slot="turn"] [data-slot="current-activity"]');
  expect(await footer.evaluate(el => el.closest('[data-slot="turn"]') !== null)).toBe(true);
  await page.emulateMedia({ reducedMotion: "reduce" });
  expect(await feedback.locator('[data-thinking-active]').evaluate(el => getComputedStyle(el).animationName)).toBe("none");
  await page.screenshot({ path: test.info().outputPath(`intent-${colorScheme}-narrow.png`), animations: "disabled" });
});

test("I5/I7/I8 background settling disables stop and updates the rail reader", async ({ page, request }) => {
  const job = { toolCallId: "cmd", kind: "terminal_command" as const, label: "检查导出", status: "running" as const, startedAt: "2026-01-01", endedAt: null };
  const command = toolCallPart("cmd", "execute_terminal", { status: "pending", intent: "检查导出", args: { command: "knip" }, background: { kind: "terminal_command", label: "检查导出", startedAt: "2026-01-01", endedAt: null }, display_data: { kind: "terminal-result", command: "knip", output: "first\n" } });
  const state = model([command], "reasoning"); state.background_tasks = [job];
  await seed(request, state); await page.goto("/sessions/42");
  const head = page.locator('[data-slot="activity-head"]'); await head.click();
  const row = page.locator('[data-slot="turn"] [data-tool-call-id="cmd"]');
  await expect(row).toContainText("后台运行中"); await expect(row).not.toContainText("准备执行");
  const board = page.locator('[data-background-task="cmd"]');
  await board.locator("button[aria-expanded]").click();
  const readerButton = board.getByRole("button", { name: "查看输出" });
  await readerButton.click();
  const dialog = page.getByRole("dialog"); await expect(dialog).toBeVisible();
  const settling = { ...state, background_tasks: [{ ...job, status: "settling" as const }], messages: [state.messages[0], assistantMessage("a", 1, [{ ...command, display_data: { ...command.display_data, output: "first\nlast\n" } }])] };
  await seed(request, settling);
  await expect(dialog).toContainText("后台结果收尾中");
  await expect(dialog).toContainText("last");
  await page.keyboard.press("Escape"); await expect(dialog).toBeHidden();
  await expect(readerButton).toBeFocused();
  await expect(row.getByRole("button", { name: "停止终端命令" })).toHaveCount(0);
  await expect(row).toContainText("后台结果收尾中");
  const done = { ...settling, activity: activity(1, "completed"), background_tasks: [{ ...job, status: "completed" as const }], messages: [state.messages[0], assistantMessage("a", 1, [{ ...command, status: "completed" }, { type: "text" as const, text: "验收完成", finalReply: true }], outcome("completed"))] };
  await seed(request, done); await expect(page.locator('[data-slot="current-activity"]')).toHaveCount(0);
});
