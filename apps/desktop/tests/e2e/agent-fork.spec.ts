import { expect, test } from "@playwright/test";

import type { TransportState } from "../../lib/assistant/contract";
import { seedState } from "./transport-fixtures";
import { API_ORIGIN as origin } from "./e2e-env";

const LAUNCH = { modelConfigId: null, reasoningEffort: null, disabledToolGroups: [] };

/** 主对话的一轮：Agent 调用 fork_task 分出一条侧栏对话，卡片带分叉的标题与 Task id。 */
const parentState: TransportState = {
  title: "主任务",
  messages: [
    {
      id: "1", role: "user", runId: 1, outcome: null, source: null,
      parts: [{ type: "text", text: "前后端一起改", status: "completed" }],
    },
    {
      id: "assistant-1", role: "assistant", runId: 1, source: null,
      outcome: { status: "completed", endReason: "stop", error: null, usage: null, contextWindowTotal: null, durationMs: null },
      parts: [{
        type: "tool-call", awaitingUser: false, toolCallId: "call-fork", toolName: "fork_task", status: "completed",
        args: { title: "前端部分", message: "只做前端" }, child_task_id: 41,
        display_data: { kind: "fork-task-result", title: "前端部分", child_task_id: 41 },
      }],
    },
  ],
  messageOffset: 0,
  activity: { runId: 1, status: "completed", endReason: "stop", error: null, usage: null, contextWindowTotal: null, launch: LAUNCH, startedAt: "2026-10-27T00:00:00.000Z" },
  terminal_sessions: [], background_tasks: [], foreground_waits: [], handoffs: [],
};

test.beforeEach(async ({ request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: 9, title: "主任务" } });
});

test("Agent 分出的对话：卡片出现时侧栏补上它，「打开」跳到它的对话", async ({ page, request }) => {
  await page.goto("/tasks/9");
  const sidebar = page.getByRole("navigation");
  await expect(sidebar.getByText("主任务")).toBeVisible();

  // 分叉在主对话运行中建出：页面已加载、侧栏目录里还没有它。
  await request.post(`${origin}/__test__/seed-task`, {
    data: {
      taskId: 41, title: "前端部分", taskType: "fork",
      extra: { fork: { source_task_id: 9, source_message_id: null, created_by: "agent", tool_call_id: "call-fork" } },
    },
  });
  await seedState(request, 9, parentState);

  await expect(sidebar.getByText("前端部分")).toBeVisible();
  await expect(sidebar.getByLabel("Agent 分出的对话")).toBeVisible();

  await page.getByRole("button", { name: "打开分叉对话：前端部分" }).click();
  await expect(page).toHaveURL(/\/tasks\/41$/);
});
