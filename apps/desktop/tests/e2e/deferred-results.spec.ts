import { expect, test } from "@playwright/test";

import type { DeferredResultEntry } from "@/lib/assistant/contract";

import { completedState, seedState } from "./transport-fixtures";

const origin = "http://127.0.0.1:8000";
const TASK_ID = 61;

function entry(toolCallId: string, overrides: Partial<DeferredResultEntry> = {}): DeferredResultEntry {
  return {
    toolCallId,
    kind: "terminal_wait",
    label: `wait ${toolCallId}`,
    mode: "background",
    status: "running",
    startedAt: new Date(Date.now() - 40_000).toISOString(),
    endedAt: null,
    ...overrides,
  };
}

test.beforeEach(async ({ request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: TASK_ID, title: "后台任务" } });
  await seedState(request, TASK_ID, completedState(
    1,
    { id: "user-1", parts: [{ type: "text", text: "跑一下构建" }] },
    [
      { type: "tool-call", toolCallId: "call-bg", toolName: "terminal_read", args: {}, status: "pending", deferred: true },
      { type: "tool-call", toolCallId: "call-fg", toolName: "terminal_read", args: {}, status: "running" },
    ],
  ));
  await request.post(`${origin}/__test__/seed-deferred`, {
    data: {
      taskId: TASK_ID,
      entries: [
        entry("call-bg", { label: "cd /work && make" }),
        entry("call-fg", { mode: "foreground", label: "npm test" }),
        entry("call-old", { status: "completed", endedAt: new Date().toISOString() }),
      ],
    },
  });
});

test("任务概览列出后台任务，卡片与看板都能取消或转到后台", async ({ page }) => {
  await page.goto(`/tasks/${TASK_ID}`);
  const entryButton = page.getByRole("button", { name: /^任务概览，2 个后台任务运行中/ });
  await expect(entryButton).toBeVisible();
  await entryButton.click();

  const card = page.getByRole("region", { name: "后台任务" });
  await expect(card).toContainText("cd /work && make");
  await expect(card).toContainText("npm test");
  await expect(card).toContainText("1 已完成");

  // 后台占位卡片：「后台执行中」+ 取消；取消后占位推到 cancelled，看板条目结束。连续工具调用默认收成一组。
  await page.getByRole("button", { name: /个工具调用/ }).click();
  const bgActions = page.locator('[data-deferred-actions="call-bg"]');
  await expect(bgActions).toContainText("后台执行中");
  await bgActions.getByRole("button", { name: "取消" }).click();
  await expect(bgActions).toHaveCount(0);
  await expect(card).toContainText("2 已完成");
  await expect(page.getByRole("button", { name: /^任务概览，1 个后台任务运行中/ })).toBeVisible();

  // 前台等待：看板行上「转到后台」，转走后变为后台条目，不再提供转后台。
  const fgRow = page.locator('[data-deferred-entry="call-fg"]');
  await fgRow.getByRole("button", { name: "转到后台" }).click();
  await expect(fgRow.getByRole("button", { name: "转到后台" })).toHaveCount(0);
  await expect(fgRow.getByRole("button", { name: "停止" })).toBeVisible();
});
