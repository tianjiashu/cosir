import { expect, test } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import type { APIRequestContext } from "@playwright/test";
import type { TransportState } from "../../lib/assistant/contract";
const seedState = (request: APIRequestContext, taskId: number, state: TransportState) => request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { taskId, state, hold: true } });
import { activity, assistantMessage, outcome, toolCallPart, transportState, userMessage } from "../unit/transport-fixtures";
import type { TransportMessagePart, TransportOutcome } from "../../lib/assistant/contract";
const text = (value: string, finalReply = false): TransportMessagePart => ({ type: "text", text: value, finalReply });
const call = (id: string, standalone = false) => toolCallPart(id, standalone ? "fork_task" : "read_file", { presentation: standalone ? "standalone" : "activity", status: "completed" });
const state = (parts: TransportMessagePart[], end: TransportOutcome | null = null) => transportState({
  messages: [userMessage("u", 1, "时间线验收"), assistantMessage("a", 1, parts, end)],
  activity: activity(1, end?.status ?? "running", { endReason: end?.endReason ?? null, startedAt: new Date(Date.now() - 8000).toISOString() }),
});

test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 42, title: "时间线验收" } });
});

for (const colorScheme of ["light", "dark"] as const) test(`A1–A3 ${colorScheme}: standalone folds with process; hover does not pin; final DOM and manual preference survive`, async ({ page, request }) => {
  await page.emulateMedia({ colorScheme });
  const parts = [text("调研进展"), call("read"), call("fork", true), text("最终结论")];
  await seedState(request, 42, state(parts));
  await page.goto("/tasks/42");
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

for (const [status, reason] of [["completed", "stop"], ["completed", "length"], ["completed", "forked"], ["cancelled", "runtime_restarted"], ["cancelled", "user_cancelled"], ["failed", "error"]] as const) test(`A4 ${status}/${reason}: no final remains open`, async ({ page, request }) => {
  await seedState(request, 42, state([text("已产生的进展"), call("fork", true)], outcome(status, { endReason: reason })));
  await page.goto("/tasks/42");
  await expect(page.locator('[data-slot="process-head"]')).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("已产生的进展", { exact: true })).toBeVisible();
  await expect(page.locator('[data-thinking-active="true"]')).toHaveCount(0);
});

test("A5/A10 running shimmer survives hidden process, is static for reduced motion and suspended", async ({ page, request }) => {
  const running = toolCallPart("read", "read_file", { status: "running" });
  await seedState(request, 42, state([running]));
  await page.goto("/tasks/42");
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

const preview = { kind: "agent-team-approval", team_run_id: 17, team_id: "review", name: "代码检查", goal: "检查", start_node: "a", configuration: { team_id: "review" }, node_goals: { a: { goal: "检查实现", summary: "检查" } }, edges: [], nodes: [{ node_id: "a", name: "审查", role: "Reviewer", statuses: [{ name: "完成", color: "#888888" }], max_steps: 5 }] };
const teamPart = () => toolCallPart("team", "agent_team", { presentation: "standalone", status: "pending", awaitingUser: true, team_run_id: 17, display_data: preview });

test("A6/A7 pending draft survives disclosure and task switch; approved timeline opens existing background details", async ({ page, request }) => {
  let teamStatus = "pending";
  await page.route("**/agent-team/runs/17", route => route.fulfill({ json: { id: 17, team_id: "review", goal: "检查", status: teamStatus, end_reason: null, current_node_id: null, nodes: [] } }));
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 43, title: "另一个任务" } });
  await seedState(request, 42, state([teamPart()]));
  await page.goto("/tasks/42");
  const form = page.locator('[data-slot="agent-team-approval"]');
  await expect(form).toHaveCount(1);
  await page.getByLabel("Agent Team 目标", { exact: true }).fill("用户修改过的目标");
  const row = page.locator('[data-slot="turn"] [data-tool-call-id="team"]');
  await row.locator('[data-slot="call-row"]').click();
  await expect(form).toBeHidden();
  await row.locator('[data-slot="call-row"]').click();
  await expect(page.getByLabel("Agent Team 目标", { exact: true })).toHaveValue("用户修改过的目标");
  await page.getByRole("treeitem").filter({ hasText: "另一个任务" }).click();
  await page.getByRole("treeitem").filter({ hasText: "时间线验收" }).click();
  await expect(page.getByLabel("Agent Team 目标", { exact: true })).toHaveValue("用户修改过的目标");
  await expect(page.locator('[data-background-task="team"]')).toHaveCount(0);
  let submitted: unknown;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/tasks/42/tool-calls/team/decision", async route => { submitted = route.request().postDataJSON(); await gate; teamStatus = "running"; await route.fulfill({ json: {} }); });
  await page.getByRole("button", { name: "确认执行", exact: true }).click();
  await expect.poll(() => submitted).toBeTruthy();
  expect(JSON.stringify(submitted)).toContain("用户修改过的目标");
  await expect(form.getByRole("button", { name: "正在提交…", exact: true }).last()).toBeDisabled();
  release();
  const startedAt = new Date().toISOString();
  await seedState(request, 42, state([{ ...teamPart(), awaitingUser: false, background: { kind: "agent_team", label: "代码检查", startedAt, endedAt: null } }]));
  await request.post(`${API_ORIGIN}/__test__/seed-background-tasks`, { data: { taskId: 42, entries: [{ toolCallId: "team", kind: "agent_team", label: "代码检查", status: "running", startedAt, endedAt: null }] } });
  await expect(form).toBeHidden();
  await expect(row).toContainText("执行中");
  await expect(row.getByRole("region", { name: "Agent Team 执行进度" })).toHaveCount(0);
  await row.getByRole("button", { name: "查看", exact: true }).click();
  await expect(page.locator('[data-background-task="team"]').getByRole("region", { name: "Agent Team 执行进度" })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("team-background.png") });
});

for (const [status, endReason, label] of [["completed", null, "已完成"], ["failed", "error", "执行失败"], ["cancelled", "user_rejected", "已驳回"]] as const) test(`A7 Team ${status}/${endReason}: entity outcome is static and never an approval form`, async ({ page, request }) => {
  await page.route("**/agent-team/runs/17", route => route.fulfill({ json: { id: 17, team_id: "review", goal: "检查", status, end_reason: endReason, nodes: [], current_node_id: null } }));
  await seedState(request, 42, state([{ ...teamPart(), status: "completed", awaitingUser: false }], outcome("completed")));
  await page.goto("/tasks/42");
  const row = page.locator('[data-slot="turn"] [data-tool-call-id="team"]');
  await expect(row).toContainText(label);
  await expect(page.locator('[data-slot="agent-team-approval"]')).toHaveCount(0);
  await expect(row.locator('[data-thinking-active="true"]')).toHaveCount(0);
});

test("A10 keyboard focus inside process defers automatic folding until focus leaves", async ({ page, request }) => {
  const parts = [call("read"), text("答复")];
  await seedState(request, 42, state(parts)); await page.goto("/tasks/42");
  await page.locator('[data-slot="activity-head"]').focus();
  await seedState(request, 42, state([parts[0], text("答复", true)], outcome("completed")));
  await expect(page.locator('[data-slot="process-head"]')).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator('[data-slot="activity-head"]')).toBeFocused();
  await page.getByRole("textbox", { name: "消息输入" }).focus();
  await expect(page.locator('[data-slot="process-head"]')).toHaveAttribute("aria-expanded", "false");
});
