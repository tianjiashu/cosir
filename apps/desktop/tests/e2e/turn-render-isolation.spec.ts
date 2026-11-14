import { expect, test } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { activity, assistantMessage, outcome, toolCallPart, transportState, userMessage } from "../unit/transport-fixtures";

test("耗时只刷新标题；正文增量不重绘未变工具，工具终态仍立即更新", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-session`, { data: { sessionId: 42, title: "渲染隔离" } });
  const call = toolCallPart("read", "read_file", { status: "running", display_data: { kind: "read-file-meta", file_size: 12, line_range: { start: 1, end: 2 } } });
  const state = transportState({
    messages: [userMessage("u", 1), assistantMessage("a", 1, [call, { type: "text", text: "正在解释", status: "running" }])],
    activity: activity(1, "running", { startedAt: new Date(Date.now() - 5000).toISOString() }),
  });
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { sessionId: 42, state, hold: true } });
  await page.goto("/sessions/42");
  await page.locator('[data-slot="activity-head"]').click();
  const row = page.locator('[data-slot="turn"] [data-tool-call-id="read"]');
  await expect(row).toBeVisible();
  // 只在测试浏览器里包装现有视图函数计数，不向生产代码加入测试/性能接口。
  await page.evaluate(async () => {
    const modulePath = "/components/conversation/tools/call-view-registry.ts";
    const { CALL_VIEWS } = await import(/* @vite-ignore */ modulePath);
    const original = CALL_VIEWS["read-file-meta"];
    const counter = { renders: 0 };
    Object.assign(window, { toolRenderCounter: counter });
    CALL_VIEWS["read-file-meta"] = (...args: unknown[]) => { counter.renders++; return original(...args); };
  });
  const renders = () => page.evaluate(() => (window as unknown as { toolRenderCounter: { renders: number } }).toolRenderCounter.renders);
  const heading = page.locator('[data-slot="turn-status"]');
  const before = await heading.textContent();
  await expect.poll(() => heading.textContent()).not.toBe(before);
  expect(await renders()).toBe(0);

  await request.post(`${API_ORIGIN}/__test__/append-text`, { data: { sessionId: 42, messageIndex: 1, partIndex: 1, text: "，继续正文" } });
  await expect(page.getByText("正在解释，继续正文", { exact: true })).toBeVisible();
  expect(await renders()).toBe(0);

  // 键盘焦点在过程内时会有意阻止自动收起；先回到输入框再验证完成后的默认折叠。
  await page.getByRole("textbox", { name: "消息输入" }).focus();
  const end = outcome("completed", { durationMs: 12000 });
  state.activity = activity(1, "completed");
  state.messages[1] = assistantMessage("a", 1, [{ ...call, status: "completed" }, { type: "text", text: "最终答复", finalReply: true, status: "completed" }], end);
  await request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { sessionId: 42, state } });
  await expect(heading).toHaveText("用时 12秒");
  await expect(page.locator('[data-slot="process-head"]')).toHaveAttribute("aria-expanded", "false");
  await page.locator('[data-slot="process-head"]').click();
  await expect(page.locator('[data-slot="activity-head"]')).toHaveAttribute("aria-expanded", "true");
  await expect(row).toHaveAttribute("data-tool-status", "completed");
  expect(await renders()).toBeGreaterThan(0);
  await page.clock.install();
  await page.clock.fastForward(3000);
  await expect(heading).toHaveText("用时 12秒");
});
