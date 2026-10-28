import { expect, test } from "@playwright/test";

import type { BackgroundTaskEntry } from "@/lib/assistant/contract";

import { completedState, seedState } from "./transport-fixtures";

const origin = "http://127.0.0.1:8000";
const TASK_ID = 61;

function entry(toolCallId: string, overrides: Partial<BackgroundTaskEntry> = {}): BackgroundTaskEntry {
  return {
    toolCallId,
    kind: "terminal_wait",
    label: `wait ${toolCallId}`,
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
  await request.post(`${origin}/__test__/seed-background-tasks`, {
    data: {
      taskId: TASK_ID,
      entries: [
        entry("call-bg", { label: "cd /work && make" }),
        entry("call-old", { status: "completed", endedAt: new Date().toISOString() }),
      ],
      foregroundWaits: [entry("call-fg", { label: "npm test" })],
    },
  });
});

test("看板只列后台工作；前台等待在卡片上转到后台后才进看板", async ({ page }) => {
  await page.goto(`/tasks/${TASK_ID}`);
  const entryButton = page.getByRole("button", { name: /^任务概览，1 个后台任务运行中/ });
  await expect(entryButton).toBeVisible();
  await entryButton.click();

  const card = page.getByRole("region", { name: "后台任务" });
  await expect(card).toContainText("cd /work && make");
  await expect(card).not.toContainText("npm test");
  await expect(card).toContainText("1 已完成");

  // 后台占位卡片：「后台执行中」+ 取消；取消后占位推到 cancelled，看板条目结束。连续工具调用默认收成一组。
  await page.getByRole("button", { name: /个工具调用/ }).click();
  const bgActions = page.locator('[data-deferred-actions="call-bg"]');
  await expect(bgActions).toContainText("后台执行中");
  await bgActions.getByRole("button", { name: "取消" }).click();
  await expect(bgActions).toHaveCount(0);
  await expect(card).toContainText("2 已完成");

  // 前台等待：只在工具卡片上「转到后台」；转走后才出现在看板，卡片不再提供转后台。
  const fgActions = page.locator('[data-deferred-actions="call-fg"]');
  await fgActions.getByRole("button", { name: "转到后台" }).click();
  await expect(fgActions).toHaveCount(0);
  const fgRow = page.locator('[data-background-task="call-fg"]');
  await expect(fgRow.getByRole("button", { name: "停止" })).toBeVisible();
  await expect(page.getByRole("button", { name: /^任务概览，1 个后台任务运行中/ })).toBeVisible();
});
