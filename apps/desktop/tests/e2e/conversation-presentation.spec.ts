import { expect, test } from "@playwright/test";
import { API_ORIGIN, OUTPUT_DIR } from "./e2e-env";
import { seedState } from "./transport-fixtures";
import { activity, agentMessage, assistantMessage, outcome, toolCallPart, transportState, userMessage } from "../unit/transport-fixtures";
import type { TransportMessagePart } from "../../lib/assistant/contract";
const text = (value: string, finalReply = false): TransportMessagePart => ({ type: "text", text: value, finalReply });
const state = (parts: TransportMessagePart[], status: "running" | "suspended" | "completed" | "cancelled" = "running") => transportState({
  messages: [userMessage("u", 1, "检查项目"), assistantMessage("a", 1, parts, status === "completed" || status === "cancelled" ? outcome(status, { durationMs: 8000 }) : null)],
  activity: activity(1, status, { endReason: status === "completed" ? "stop" : status === "cancelled" ? "user_cancelled" : null }),
});
test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 42, title: "界面改造验证" } });
});

test("live snapshot updates preserve the original prose DOM and selection; stopped content remains readable", async ({ page, request }) => {
  const prose = text("先检查取消处理，再核对结果。");
  await seedState(request, 42, state([prose], "suspended"));
  await page.goto("/tasks/42");
  const slot = page.locator('[data-slot="prose-part"]').first();
  await expect(slot).toBeVisible();
  await slot.evaluate((element) => {
    (window as unknown as { originalProse: Element }).originalProse = element;
    const range = document.createRange(); range.selectNodeContents(element);
    getSelection()?.removeAllRanges(); getSelection()?.addRange(range);
  });
  await seedState(request, 42, state([prose, toolCallPart("read", "read_file", { status: "completed" }), text("已经找到处理入口。") ]));
  await expect(page.getByText("已经找到处理入口。", { exact: true })).toBeVisible();
  expect(await slot.evaluate((element) => element === (window as unknown as { originalProse: Element }).originalProse)).toBe(true);
  expect(await page.evaluate(() => getSelection()?.toString())).toContain("先检查取消处理");
  await seedState(request, 42, state([prose, toolCallPart("read", "read_file", { status: "cancelled" }), text("已经找到处理入口。")], "cancelled"));
  await expect(page.locator('[data-slot="turn-status"]')).toContainText("已停止");
  await expect(page.locator('[data-slot="turn-status"]')).not.toContainText("有异常记录");
  await expect(page.locator('[data-slot="activity-head"]')).toContainText("1 个取消");
  await expect(slot).toBeVisible();
  await expect(page.getByRole("button", { name: "复制已生成内容", exact: true })).toBeVisible();
  await expect(page.getByText("回复可能尚未完成。", { exact: true })).toBeVisible();
});

test("wait does not render a card; suspended has a neutral heading and errors stay discoverable", async ({ page, request }) => {
  const wait = toolCallPart("wait", "wait_messages", { status: "completed", presentation: "quiet" });
  await seedState(request, 42, state([text("等待结果回来。"), wait], "suspended"));
  await page.goto("/tasks/42");
  await expect(page.locator('[data-slot="current-activity"]')).toContainText("等待继续");
  await expect(page.locator('[data-slot="activity-group"]')).toHaveCount(0);
  await expect(page.locator('[data-tool-call-id="wait"]')).toHaveCount(0);
  await seedState(request, 42, state([text("等待结果回来。"), { ...wait, status: "failed", error: "没有可等待的任务" }], "suspended"));
  await expect(page.locator('[data-slot="activity-head"]')).toContainText("1 个失败");
  await page.locator('[data-slot="activity-head"]').click();
  await expect(page.locator('[data-tool-call-id="wait"]')).toContainText("没有可等待的任务");
});

test("Agent messages are compact, explicitly expandable and never human edit targets", async ({ page, request }) => {
  const message = agentMessage("incoming", 1, "需要检查取消路径。\n\n原始详细说明第二段。");
  await seedState(request, 42, transportState({ messages: [message, assistantMessage("a", 1, [text("我继续核对。")], outcome("completed"))] }));
  await page.goto("/tasks/42");
  const incoming = page.locator('[data-role="agent-message"]');
  await expect(incoming).toContainText("来自 主任务");
  await expect(incoming.locator('[data-slot="agent-message-body"]')).toHaveCount(0);
  await incoming.getByRole("button", { name: /来自/ }).click();
  await expect(incoming.locator('[data-slot="agent-message-body"]')).toContainText("原始详细说明第二段。");
  await expect(incoming.getByRole("button", { name: "打开来源任务" })).toBeVisible();
  await expect(incoming.getByRole("button", { name: /编辑|重跑/ })).toHaveCount(0);
  await page.screenshot({ path: `${OUTPUT_DIR}/conversation-agent.png`, animations: "disabled" });
});

test("completed reply folds prose in place while preserving result warning in the summary", async ({ page, request }) => {
  await seedState(request, 42, state([text("正在运行测试。"), toolCallPart("cmd", "execute_terminal", { status: "completed", display_data: { kind: "terminal-result", command: "test", output: "assertion", exit_code: 1 } }), text("测试有一个断言需要修复。", true)], "completed"));
  await page.goto("/tasks/42");
  await expect(page.getByText("正在运行测试。", { exact: true })).toBeHidden();
  await expect(page.getByText("测试有一个断言需要修复。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "用时 8秒 · 有异常记录，展开过程" })).toBeVisible();
  await expect(page.locator('[data-slot="turn-status"]')).not.toContainText(/执行命令|次/);
  await page.locator('[data-slot="process-head"]').click();
  await expect(page.getByText("正在运行测试。", { exact: true })).toBeVisible();
  await page.locator('[data-slot="activity-head"]').click();
  await expect(page.locator('[data-tool-call-id="cmd"]')).toContainText("非零退出 1");
  await page.screenshot({ path: `${OUTPUT_DIR}/conversation-result.png`, animations: "disabled" });
});

test("stop request is disabled across unmount until the canonical outcome arrives", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/seed-task`, { data: { taskId: 43, title: "另一个任务" } });
  await seedState(request, 42, state([text("已完成部分检查。")], "suspended"));
  await page.route(`${API_ORIGIN}/tasks/42/cancel`, (route) => route.fulfill({ status: 202, contentType: "application/json", body: "{}" }));
  await page.goto("/tasks/42");
  await page.getByRole("button", { name: "停止", exact: true }).click();
  await expect(page.getByRole("button", { name: "正在停止", exact: true })).toBeDisabled();
  await expect(page.locator('[data-slot="current-activity"]')).toContainText("正在停止");
  await page.getByRole("treeitem").filter({ hasText: "另一个任务" }).click();
  await expect(page).toHaveURL(/tasks\/43$/);
  await page.getByRole("treeitem").filter({ hasText: "界面改造验证" }).click();
  await expect(page).toHaveURL(/tasks\/42$/);
  await expect(page.getByRole("button", { name: "正在停止", exact: true })).toBeDisabled();
  await seedState(request, 42, state([text("已完成部分检查。")], "cancelled"));
  await expect(page.locator('[data-slot="turn-status"]')).toContainText("已停止");
  await expect(page.getByText("已完成部分检查。", { exact: true })).toBeVisible();
});

test("an unknown stop result offers explicit retry without claiming a failed execution", async ({ page, request }) => {
  await seedState(request, 42, state([text("保留当前进展。")], "suspended"));
  let calls = 0;
  await page.route(`${API_ORIGIN}/tasks/42/cancel`, route => ++calls === 1
    ? route.abort("failed") : route.fulfill({ status: 202, contentType: "application/json", body: "{}" }));
  await page.goto("/tasks/42");
  await page.getByRole("button", { name: "停止", exact: true }).click();
  await expect(page.getByRole("button", { name: "重试停止", exact: true }).first()).toBeEnabled();
  await expect(page.getByText("停止结果待确认", { exact: true })).toBeVisible();
  expect(calls).toBe(1);
  await page.locator(".aui-composer-cancel").click();
  await expect(page.getByRole("button", { name: "正在停止", exact: true })).toBeDisabled();
  expect(calls).toBe(2);
});

test("a late stop response cannot make a new Run appear to be stopping", async ({ page, request }) => {
  await seedState(request, 42, state([text("旧运行。")], "suspended"));
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route(`${API_ORIGIN}/tasks/42/cancel`, async route => {
    await held;
    await route.fulfill({ status: 202, contentType: "application/json", body: "{}" });
  });
  await page.goto("/tasks/42");
  await page.getByRole("button", { name: "停止", exact: true }).click();
  await expect(page.getByRole("button", { name: "正在停止", exact: true })).toBeDisabled();
  await seedState(request, 42, transportState({ messages: [userMessage("u2", 2), assistantMessage("a2", 2, [text("新运行。")])], activity: activity(2, "suspended") }));
  await expect(page.getByText("新运行。", { exact: true })).toBeVisible();
  const response = page.waitForResponse(`${API_ORIGIN}/tasks/42/cancel`);
  release();
  await response;
  await expect(page.getByRole("button", { name: "停止", exact: true })).toBeEnabled();
  await expect(page.locator('[data-slot="current-activity"]')).toContainText("等待继续");
});
