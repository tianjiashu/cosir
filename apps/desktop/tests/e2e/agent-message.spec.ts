import { expect, test } from "@playwright/test";

import type { TransportState } from "../../lib/assistant/contract";
import { seedState } from "./transport-fixtures";
import { API_ORIGIN as origin } from "./e2e-env";

const LAUNCH = { modelConfigId: null, reasoningEffort: null, disabledToolGroups: [] };

/** 子任务的一轮：任务说明是父 Agent 发来的 Agent 消息，不是用户说的话。 */
const snapshot: TransportState = {
  title: "子任务",
  messages: [
    {
      id: "11", role: "user", runId: 1, outcome: null,
      source: { taskId: 9, name: "主任务" },
      parts: [{ type: "text", text: "请检查登录流程", status: "completed" }],
    },
    {
      id: "assistant-11", role: "assistant", runId: 1, source: null,
      parts: [{ type: "text", text: "检查完毕", status: "completed" }],
      outcome: { status: "completed", endReason: "stop", error: null, usage: null, contextWindowTotal: null, durationMs: 0 },
    },
  ],
  messageOffset: 0,
  activity: {modelActivity: null,  runId: 1, status: "completed", endReason: "stop", error: null, usage: null, contextWindowTotal: null, launch: LAUNCH, startedAt: "2026-10-27T00:00:00.000Z" },
  terminal_sessions: [], background_tasks: [], foreground_waits: [], handoffs: [],
};

test.beforeEach(async ({ request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: 9, title: "主任务" } });
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: 42, title: "子任务" } });
  await seedState(request, 42, snapshot);
});

test("Agent 消息默认一行预览、不可编辑，展开后可跳到来源 Task", async ({ page }) => {
  await page.goto("/tasks/42");
  const sender = page.locator('[data-slot="agent-message-toggle"]');
  await expect(sender).toContainText("来自 主任务");
  await expect(sender).toContainText("请检查登录流程");
  // 不画成用户气泡：没有「编辑并重跑」入口（它是最新一条 user 消息，用户消息在此时可编辑）。
  await expect(page.locator('[data-role="agent-message"]')).toHaveCount(1);
  await expect(page.locator('[data-role="user"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: "编辑并重跑" })).toHaveCount(0);

  await sender.click();
  await page.getByRole("button", { name: "打开来源任务" }).click();
  await expect(page).toHaveURL(/\/tasks\/9$/);
});
