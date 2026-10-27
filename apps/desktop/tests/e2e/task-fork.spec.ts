import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import type { TransportState } from "../../lib/assistant/contract";
import { seedState } from "./transport-fixtures";

const origin = "http://127.0.0.1:8000";
const LAUNCH = { modelConfigId: null, reasoningEffort: null, banTools: [] };
/** 两轮回复：第一轮已结束（带结局），第二轮处于 `status`。 */
const snapshot = (status: "completed" | "running" | "suspended"): TransportState => {
  const ended = (runStatus: string) => runStatus === "completed";
  const turn = (runId: number, runStatus: string) => [
    { id: `u-${runId}`, role: "user" as const, runId, outcome: null,
      parts: [{ type: "text" as const, text: `问题 ${runId}`, status: "completed" as const }] },
    { id: `assistant-u-${runId}`, role: "assistant" as const, runId,
      parts: [{ type: "text" as const, text: `回答 ${runId}`, status: ended(runStatus) ? "completed" as const : "running" as const }],
      outcome: ended(runStatus) ? { status: "completed" as const, endReason: "stop", error: null, usage: null, contextWindowTotal: null } : null },
  ];
  return {
    title: "测试对话",
    messages: [...turn(1, "completed"), ...turn(2, status)],
    activity: { runId: 2, status, endReason: null, error: null, usage: null, contextWindowTotal: null, launch: LAUNCH },
  };
};

async function seedView(page: Page, request: APIRequestContext, status: "completed" | "running" | "suspended" = "completed") {
  await seedState(request, 42, snapshot(status));
  await page.goto("/tasks/42");
  await expect(page.getByText("回答 1", { exact: true })).toBeVisible();
}

const target = {
  task_id: 44, workspace_id: 7, title: "历史分支", task_type: "fork",
  execution_status: null, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

test.beforeEach(async ({ request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: 42, title: "源任务" } });
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: 43, title: "另一个任务" } });
});

for (const status of ["running", "suspended"] as const) {
  test(`后续 ${status} 活动不阻止 fork 已结束的那一轮`, async ({ page, request }) => {
    const calls: unknown[] = [];
    await page.route(`${origin}/tasks/42/fork`, async (route) => {
      calls.push(route.request().postDataJSON());
      await route.fulfill({ status: 409, json: { detail: { code: "WORKSPACE_BUSY", message: "busy", retryable: true } } });
    });
    await seedView(page, request, status);
    const buttons = page.getByRole("button", { name: "从此处 Fork 新任务", exact: true });
    await expect(buttons).toHaveCount(2);
    await expect(buttons.first()).toBeEnabled();
    await expect(buttons.last()).toBeDisabled();
    await buttons.first().click();
    // fork 按消息切分：入口挂在带结局的回复上，请求体只带消息 id。
    await expect.poll(() => calls).toEqual([{ messageId: "assistant-u-1" }]);
    await expect(page.getByText("工作区正在处理其他操作，请稍后重试。", { exact: true })).toBeVisible();
  });
}

test("fork 成功后导航不等待目录刷新", async ({ page, request }) => {
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: 44, title: target.title } });
  await page.route(`${origin}/tasks/42/fork`, (route) => route.fulfill({ json: target }));
  await seedView(page, request);
  // 后续目录刷新失败不能把创建成功变成失败或再次创建。
  await page.route(`${origin}/workspaces`, (route) => route.fulfill({ status: 503, body: "unavailable" }));
  await page.getByRole("button", { name: "从此处 Fork 新任务", exact: true }).first().click();
  await expect(page).toHaveURL(/\/tasks\/44$/);
  await expect(page.getByText("Fork 失败", { exact: true })).toHaveCount(0);
});

test("切换任务后 fork 的迟到响应不抢回页面", async ({ page, request }) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let submitted = false;
  await page.route(`${origin}/tasks/42/fork`, async (route) => {
    submitted = true;
    await gate;
    await route.fulfill({ json: target });
  });
  await seedView(page, request);
  await page.getByRole("button", { name: "从此处 Fork 新任务", exact: true }).first().click();
  await expect.poll(() => submitted).toBe(true);
  await page.getByRole("treeitem").filter({ hasText: "另一个任务" }).click();
  await expect(page).toHaveURL(/\/tasks\/43$/);
  release();
  await expect(page.getByText("Fork 任务已创建", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/tasks\/43$/);
});
