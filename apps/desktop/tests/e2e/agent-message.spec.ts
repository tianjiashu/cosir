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
      id: "11", role: "user", turn: 1, inherited: false, outcome: null,
      source: { taskId: 9, teamRunId: null, name: "主任务", report: null },
      parts: [{ type: "text", text: "请检查登录流程", status: "completed" }],
    },
    {
      id: "assistant-turn-1", role: "assistant", turn: 1, inherited: false, source: null,
      parts: [{ type: "text", text: "检查完毕", status: "completed" }],
      outcome: { status: "completed", endReason: "stop", error: null, usage: null, contextWindowTotal: null, durationMs: 0 },
    },
  ],
  messageOffset: 0,
  activity: {modelActivity: null, turn: 1, resumable: false, status: "completed", endReason: "stop", error: null, usage: null, contextWindowTotal: null, launch: LAUNCH, startedAt: "2026-10-27T00:00:00.000Z" },
  terminal_sessions: [], background_tasks: [], foreground_waits: [], handoffs: [],
};

test.beforeEach(async ({ request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: 9, title: "主任务" } });
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: 42, title: "子任务" } });
  await seedState(request, 42, snapshot);
});

test("途中来信不切开回复：只有一个标题和脚注，展开过程才看得到", async ({ page, request }) => {
  await seedState(request, 42, {
    ...snapshot,
    messages: [
      { id: "10", role: "user", turn: 1, inherited: false, outcome: null, source: null, parts: [{ type: "text", text: "检查登录", status: "completed" }] },
      {
        id: "assistant-turn-1", role: "assistant", turn: 1, inherited: false, source: null,
        parts: [
          { type: "text", text: "先看入口", status: "completed" },
          { type: "agent-message", id: "11", text: "顺便看注册", source: { taskId: 9, teamRunId: null, name: "主任务", report: null } },
          { type: "text", text: "都检查完了", status: "completed", finalReply: true },
        ],
        outcome: snapshot.messages[1].outcome,
      },
    ],
  });
  await page.goto("/tasks/42");
  await expect(page.locator('[data-slot="turn"]')).toHaveCount(1);
  const letter = page.locator('[data-slot="agent-message-toggle"]');
  await expect(letter).toBeHidden();
  await expect(page.getByText("都检查完了")).toBeVisible();

  await page.locator('[data-slot="process-head"]').click();
  await expect(letter).toContainText("来自 主任务");
  await expect(letter).not.toContainText("顺便看注册");
  await letter.click();
  await expect(page.locator('[data-slot="agent-message-body"]')).toContainText("顺便看注册");
  await expect(page.getByRole("button", { name: "撤回" })).toHaveCount(0);
});

test("Agent 消息默认只显示来源、不可编辑，展开后可跳到来源 Task", async ({ page }) => {
  await page.goto("/tasks/42");
  const sender = page.locator('[data-slot="agent-message-toggle"]');
  await expect(sender).toContainText("来自 主任务");
  await expect(sender).not.toContainText("请检查登录流程");
  // 不画成用户气泡：没有「撤回」入口（它是最新一条 user 消息，用户消息在此时可撤回）。
  await expect(page.locator('[data-role="agent-message"]')).toHaveCount(1);
  await expect(page.locator('[data-role="user"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: "撤回" })).toHaveCount(0);

  await sender.click();
  await page.getByRole("button", { name: "打开来源任务" }).click();
  await expect(page).toHaveURL(/\/tasks\/9$/);
});
