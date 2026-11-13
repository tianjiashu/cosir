import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import type { TransportState } from "../../lib/assistant/contract";
import { seedState } from "./transport-fixtures";
import { API_ORIGIN as origin } from "./e2e-env";

const LAUNCH = { modelConfigId: null, reasoningEffort: null, disabledToolGroups: [] };
/** 两轮回复：第一轮已结束（带结局），第二轮处于 `status`。 */
const snapshot = (status: "completed" | "running" | "suspended"): TransportState => {
  const ended = (runStatus: string) => runStatus === "completed";
  const exchange = (turn: number, runStatus: string) => [
    { id: `${turn}`, role: "user" as const, turn, inherited: false, source: null, outcome: null,
      parts: [{ type: "text" as const, text: `问题 ${turn}`, status: "completed" as const }] },
    { id: `assistant-turn-${turn}`, role: "assistant" as const, turn, inherited: false, source: null,
      parts: [{ type: "text" as const, text: `回答 ${turn}`, status: ended(runStatus) ? "completed" as const : "running" as const }],
      outcome: ended(runStatus) ? { status: "completed" as const, endReason: "stop", error: null, usage: null, contextWindowTotal: null, durationMs: 0 } : null },
  ];
  return {
    title: "测试对话",
    messages: [...exchange(1, "completed"), ...exchange(2, status)],
    messageOffset: 0, activity: {modelActivity: null, turn: 2, resumable: false, status, endReason: null, error: null, usage: null, contextWindowTotal: null, launch: LAUNCH, startedAt: "2026-10-27T00:00:00.000Z" },
    terminal_sessions: [], background_tasks: [], foreground_waits: [], handoffs: [],
  };
};

async function seedView(page: Page, request: APIRequestContext, status: "completed" | "running" | "suspended" = "completed") {
  await seedState(request, 42, snapshot(status));
  await page.goto("/sessions/42");
  await expect(page.getByText("回答 1", { exact: true })).toBeVisible();
}

const target = {
  session_id: 44, workspace_id: 7, title: "历史分支", session_type: "user_fork",
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

test.beforeEach(async ({ request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId: 42, title: "源会话" } });
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId: 43, title: "另一个会话" } });
});

for (const status of ["running", "suspended"] as const) {
  test(`${status} 时已结束的回复可以分叉，进行中的回复没有入口`, async ({ page, request }) => {
    const calls: unknown[] = [];
    await page.route(`${origin}/sessions/42/fork`, async (route) => {
      calls.push(route.request().postDataJSON());
      await route.fulfill({ status: 409, json: { detail: { code: "WORKSPACE_BUSY", message: "busy", retryable: true } } });
    });
    await seedView(page, request, status);
    const buttons = page.getByRole("button", { name: "从这里分叉", exact: true });
    await expect(buttons).toHaveCount(1);
    await buttons.click();
    await expect.poll(() => calls).toEqual([{ replyMessageId: "assistant-turn-1" }]);
    await expect(page.getByText("工作区正在处理其他操作，请稍后重试。", { exact: true })).toBeVisible();
  });
}

test("最新一条回复结束后可以分叉，用户消息上没有入口", async ({ page, request }) => {
  const calls: unknown[] = [];
  await page.route(`${origin}/sessions/42/fork`, async (route) => {
    calls.push(route.request().postDataJSON());
    await route.fulfill({ json: target });
  });
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId: 44, title: target.title } });
  await seedView(page, request);
  await expect(page.locator('[data-role="user"]').getByRole("button", { name: "从这里分叉" })).toHaveCount(0);
  await page.getByRole("button", { name: "从这里分叉", exact: true }).last().click();
  await expect.poll(() => calls).toEqual([{ replyMessageId: "assistant-turn-2" }]);
  await expect(page).toHaveURL(/\/sessions\/44$/);
});

test("回复已不能分叉时，后端的拒绝如实告诉用户", async ({ page, request }) => {
  await page.route(`${origin}/sessions/42/fork`, (route) => route.fulfill({ status: 409, json: { detail: { code: "FORK_REPLY_ACTIVE", message: "running", retryable: true } } }));
  await seedView(page, request);
  await page.getByRole("button", { name: "从这里分叉", exact: true }).last().click();
  await expect(page.getByText("这条回复还没结束，暂时不能分叉。", { exact: true })).toBeVisible();
});

test("fork 成功后导航不等待目录刷新", async ({ page, request }) => {
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId: 44, title: target.title } });
  await page.route(`${origin}/sessions/42/fork`, (route) => route.fulfill({ json: target }));
  await seedView(page, request);
  // 后续目录刷新失败不能把创建成功变成失败或再次创建。
  await page.route(`${origin}/workspaces`, (route) => route.fulfill({ status: 503, body: "unavailable" }));
  await page.getByRole("button", { name: "从这里分叉", exact: true }).first().click();
  await expect(page).toHaveURL(/\/sessions\/44$/);
  await expect(page.getByText("Fork 失败", { exact: true })).toHaveCount(0);
});

test("切换会话后 fork 的迟到响应不抢回页面", async ({ page, request }) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let submitted = false;
  await page.route(`${origin}/sessions/42/fork`, async (route) => {
    submitted = true;
    await gate;
    await route.fulfill({ json: target });
  });
  await seedView(page, request);
  await page.getByRole("button", { name: "从这里分叉", exact: true }).first().click();
  await expect.poll(() => submitted).toBe(true);
  await page.getByRole("treeitem").filter({ hasText: "另一个会话" }).click();
  await expect(page).toHaveURL(/\/sessions\/43$/);
  release();
  await expect(page.getByText("Fork 会话已创建", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/sessions\/43$/);
});
