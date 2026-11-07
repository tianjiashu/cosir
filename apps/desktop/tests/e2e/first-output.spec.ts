import { expect, test } from "@playwright/test";
import { API_ORIGIN, OUTPUT_DIR } from "./e2e-env";
import { seedState } from "./transport-fixtures";
import { activity, assistantMessage, outcome, transportState, userMessage, toolCallPart } from "../unit/transport-fixtures";
import type { TransportActivity, TransportMessagePart } from "../../lib/assistant/contract";

test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 42, title: "模型活动" } });
});

const state = (phase: NonNullable<TransportActivity["modelActivity"]>["phase"] | null, parts: TransportMessagePart[] = []) => transportState({
  messages: [userMessage("u", 1), assistantMessage("a", 1, parts)],
  activity: activity(1, "running", { modelActivity: phase ? { stepId: "step-2", phase } : null }),
});

test("model status survives tools without exposing reasoning or a disclosure", async ({ page, request }) => {
  await seedState(request, 42, transportState({ activity: activity(1, "suspended") }));
  await page.goto("/tasks/42");
  await expect(page.locator('[data-slot="current-activity"]')).toHaveText("等待继续");
  await seedState(request, 42, state("waiting"));
  const status = page.locator('[data-slot="current-activity"]');
  await expect(status).toHaveText("正在思考");
  await seedState(request, 42, state("reasoning"));
  await expect(status).toHaveText("正在思考");
  await page.screenshot({ path: `${OUTPUT_DIR}/model-thinking.png` });
  await expect(status.getByRole("button")).toHaveCount(0);
  const parts = [toolCallPart("read", "read_file", { status: "completed" })];
  await seedState(request, 42, state("waiting", parts));
  await expect(status).toHaveText("正在思考");
  await seedState(request, 42, state("reasoning", parts));
  await expect(status).toHaveText("正在思考");
  await expect(page.locator('[data-slot="reasoning-body"]')).toHaveCount(0);
  await seedState(request, 42, transportState({ activity: activity(1, "cancelled"), messages: [assistantMessage("a", 1, parts, outcome("cancelled"))] }));
  await expect(status).toHaveCount(0);
  await expect(page.locator('[data-slot="turn-status"]')).toContainText("已停止");
});

test("status exists without an assistant message", async ({ page, request }) => {
  await seedState(request, 42, transportState({ activity: activity(1, "suspended") }));
  await page.goto("/tasks/42");
  await expect(page.locator('[data-slot="current-activity"]')).toHaveText("等待继续");
  await seedState(request, 42, { ...state("reasoning"), messages: [] });
  await expect(page.locator('[data-slot="current-activity"]')).toHaveText("正在思考");
});


test("dark quiet waiting and reduced motion preserve status without a tool card", async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem("cosir.theme-preference", "dark"));
  await page.emulateMedia({ reducedMotion: "reduce" });
  await seedState(request, 42, transportState({
    messages: [userMessage("u", 1), assistantMessage("a", 1, [
      toolCallPart("wait", "wait_messages", { presentation: "quiet", status: "completed" }),
      toolCallPart("bg", "execute_terminal", { status: "pending", background: { kind: "terminal_command", label: "验证", startedAt: new Date().toISOString(), endedAt: null } }),
    ])], activity: activity(1, "suspended"),
  }));
  await request.post(`${API_ORIGIN}/__test__/seed-background-tasks`, { data: { taskId: 42, entries: [{ toolCallId: "bg", kind: "terminal_command", label: "验证", status: "running", startedAt: new Date().toISOString(), endedAt: null }] } });
  await page.goto("/tasks/42");
  const status = page.locator('[data-slot="current-activity"]');
  await expect(status).toHaveText("后台执行：验证");
  await expect(page.locator('[data-tool-call-id="wait"]')).toHaveCount(0);
  await request.post(`${API_ORIGIN}/__test__/seed-background-tasks`, { data: { taskId: 42, entries: [] } });
  await seedState(request, 42, state("reasoning"));
  await expect(status).toHaveText("正在思考");
  expect(await status.locator("span").evaluate(el => getComputedStyle(el).animationName)).toBe("none");
  await page.screenshot({ path: `${OUTPUT_DIR}/model-thinking-dark.png` });
});
