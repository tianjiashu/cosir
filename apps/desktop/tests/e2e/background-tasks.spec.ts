import { expect, test } from "@playwright/test";

import type { BackgroundTaskEntry } from "@/lib/assistant/contract";

import { completedState, seedState } from "./transport-fixtures";
import { API_ORIGIN as origin, OUTPUT_DIR } from "./e2e-env";

const TASK_ID = 61;

function entry(toolCallId: string, overrides: Partial<BackgroundTaskEntry> = {}): BackgroundTaskEntry {
  return {
    toolCallId,
    kind: "terminal_command",
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
      {presentation: "activity",  type: "tool-call", awaitingUser: false, toolCallId: "call-bg", toolName: "execute_terminal", args: {}, status: "pending", background: { kind: "terminal_command", label: "make watch", startedAt: "2026-10-28T08:00:00+00:00", endedAt: null } },
      {presentation: "activity",  type: "tool-call", awaitingUser: false, toolCallId: "call-fg", toolName: "execute_terminal", args: {}, status: "running" },
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
  const entryButton = page.getByRole("button", { name: /^会话侧栏，1 个后台任务运行中/ });
  await expect(entryButton).toBeVisible();

  const card = page.getByRole("region", { name: "后台任务" });
  await expect(card).toContainText("cd /work && make");
  await expect(card).not.toContainText("npm test");
  await expect(card).toContainText("1 已结束");
  // 停止只在外壳上；这条调用还没有展示数据，没有详情可展开。
  const bgRow = card.locator('[data-background-task="call-bg"]');
  await expect(bgRow.locator("button[aria-expanded]")).toHaveCount(0);
  await expect(bgRow.getByRole("button", { name: "停止" })).toHaveCount(1);

  // 后台占位行不另起状态行；取消走看板。回合已结束，过程区默认收起，先展开。
  // 没有末段回复时保留过程；普通调用在工具组展开后可操作。
  await expect(page.locator('[data-foreground-wait="call-bg"]')).toHaveCount(0);
  await page.locator('[data-background-task="call-bg"]').getByRole("button", { name: "停止" }).click();
  await expect(card).toContainText("2 已结束");

  // 前台等待：只在工具卡片上「转到后台」；转走后才出现在看板，卡片不再提供转后台。
  await page.locator('[data-slot="activity-head"]').click();
  const fgActions = page.locator('[data-foreground-wait="call-fg"]');
  await fgActions.getByRole("button", { name: "转到后台" }).click();
  await expect(fgActions).toHaveCount(0);
  const fgRow = page.locator('[data-background-task="call-fg"]');
  await expect(fgRow.getByRole("button", { name: "停止" })).toBeVisible();
  await expect(page.getByRole("button", { name: /^会话侧栏，1 个后台任务运行中/ })).toBeVisible();
});

test("Team 批准后时间线只留一行，点行进入后台详情", async ({ page, request }) => {
  const callId = "review-call";
  const preview = {
    kind: "agent-team-approval", team_run_id: 17, team_id: "review", name: "代码审查",
    goal: "审查改动", start_node: "review", configuration: { team_id: "review" },
    node_goals: { review: { goal: "检查改动", summary: "审查" } }, edges: [],
    nodes: [{ node_id: "review", name: "审查", role: "Reviewer", statuses: [{ name: "done", color: "#16A34A" }], max_steps: 5 }],
  };
  const work = { kind: "agent_team" as const, label: preview.name, startedAt: new Date().toISOString(), endedAt: null };
  let approved = false;
  async function publish() {
    await request.post(`${origin}/__test__/seed-background-tasks`, {
      data: { taskId: TASK_ID, entries: approved ? [entry(callId, work)] : [], foregroundWaits: [] },
    });
    await seedState(request, TASK_ID, completedState(1,
      { id: "user-1", parts: [{ type: "text", text: "审查改动" }] },
      [{presentation: "standalone",  type: "tool-call", toolName: "agent_team", toolCallId: callId, args: { team_id: "review", goal: preview.goal }, status: "pending",
        awaitingUser: !approved, display_data: preview, team_run_id: 17, ...(approved ? { background: work } : {}) }],
    ));
  }
  await page.route("**/agent-team/runs/17", route => route.fulfill({ json: {
    id: 17, team_id: "review", goal: "审查改动", status: approved ? "running" : "pending",
    nodes: [], current_node_id: null, end_reason: null,
  } }));
  await page.route(`**/tool-calls/${callId}/decision`, async route => {
    expect(route.request().postDataJSON().decision).toBe("approve");
    approved = true;
    await route.fulfill({ json: {} });
    await publish();
  });
  await publish();
  await page.goto(`/tasks/${TASK_ID}`);
  await expect(page.getByRole("button", { name: "确认执行" })).toBeVisible();
  await page.screenshot({ path: `${OUTPUT_DIR}/team-approval.png` });
  await page.getByRole("button", { name: "确认执行" }).click();
  await expect(page.getByRole("button", { name: "确认执行" })).toHaveCount(0);
  const timeline = page.locator(`[data-slot="turn"] [data-tool-call-id="${callId}"]`);
  await expect(timeline.locator('[data-slot="call-title"]')).toHaveAttribute("data-thinking-active", "true");
  await expect(timeline.getByRole("region", { name: "Agent Team 执行进度" })).toHaveCount(0);
  await timeline.locator('[data-slot="call-row"]').click();
  const row = page.locator(`[data-background-task="${callId}"]`);
  await expect(row).toContainText("代码审查");
  const progress = row.getByRole("region", { name: "Agent Team 执行进度" });
  await expect(progress).toBeVisible();
  const trigger = row.locator("button[aria-expanded]");
  await trigger.click();
  await expect(progress).toBeHidden();
  await trigger.focus(); await page.keyboard.press("Enter");
  await expect(progress).toBeVisible();
  await page.screenshot({ path: `${OUTPUT_DIR}/team-running.png` });
  await expect(row.getByRole("button", { name: "停止" })).toBeVisible();
  await expect(page.getByRole("button", { name: "转到后台" })).toHaveCount(0);
});
