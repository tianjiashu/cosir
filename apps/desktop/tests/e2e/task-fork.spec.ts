import { expect, test, type Page } from "@playwright/test";

const origin = "http://127.0.0.1:8000";
const snapshot = (status: string) => ({
  runs: ["completed", status].map((runStatus, index) => ({
    runId: index + 1,
    status: runStatus,
    endReason: null,
    messages: [
      { id: `u-${index}`, role: "user", parts: [{ type: "text", text: `问题 ${index + 1}`, status: "completed" }] },
      { id: `a-${index}`, role: "assistant", parts: [{ type: "text", text: `回答 ${index + 1}`, status: runStatus === "running" ? "running" : "completed" }] },
    ],
    usage: null,
    error: null,
  })),
  current_run_id: 2,
  approvals: {},
  context_window_total: null,
  error: null,
});

async function seedView(page: Page, status = "completed") {
  await page.route(`${origin}/tasks/42/assistant/state`, (route) => route.fulfill({ json: snapshot(status) }));
  await page.route(`${origin}/tasks/42/assistant/attach`, (route) => route.fulfill({
    contentType: "text/event-stream",
    body: `data: ${JSON.stringify({
      task_id: 42, kind: "full", mutations: [], state: snapshot(status), target_run_id: 2, target_run_status: status,
    })}\n\ndata: [DONE]\n\n`,
  }));
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

for (const status of ["running", "pending", "suspended"]) {
  test(`后续 ${status} Run 不阻止 fork 已结束历史`, async ({ page }) => {
    const calls: unknown[] = [];
    await page.route(`${origin}/tasks/42/fork`, async (route) => {
      calls.push(route.request().postDataJSON());
      await route.fulfill({ status: 409, json: { detail: { code: "WORKSPACE_BUSY", message: "busy", retryable: true } } });
    });
    await seedView(page, status);
    const buttons = page.getByRole("button", { name: "从此处 Fork 新任务", exact: true });
    await expect(buttons).toHaveCount(2);
    await expect(buttons.first()).toBeEnabled();
    await expect(buttons.last()).toBeDisabled();
    await buttons.first().click();
    await expect.poll(() => calls).toEqual([{ runId: 1 }]);
    await expect(page.getByText("工作区正在处理其他操作，请稍后重试。", { exact: true })).toBeVisible();
  });
}

test("fork 成功后导航不等待目录刷新", async ({ page, request }) => {
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: 44, title: target.title } });
  await page.route(`${origin}/tasks/42/fork`, (route) => route.fulfill({ json: target }));
  await seedView(page);
  // 后续目录刷新失败不能把创建成功变成失败或再次创建。
  await page.route(`${origin}/workspaces`, (route) => route.fulfill({ status: 503, body: "unavailable" }));
  await page.getByRole("button", { name: "从此处 Fork 新任务", exact: true }).first().click();
  await expect(page).toHaveURL(/\/tasks\/44$/);
  await expect(page.getByText("Fork 失败", { exact: true })).toHaveCount(0);
});

test("切换任务后 fork 的迟到响应不抢回页面", async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let submitted = false;
  await page.route(`${origin}/tasks/42/fork`, async (route) => {
    submitted = true;
    await gate;
    await route.fulfill({ json: target });
  });
  await seedView(page);
  await page.getByRole("button", { name: "从此处 Fork 新任务", exact: true }).first().click();
  await expect.poll(() => submitted).toBe(true);
  await page.getByRole("treeitem").filter({ hasText: "另一个任务" }).click();
  await expect(page).toHaveURL(/\/tasks\/43$/);
  release();
  await expect(page.getByText("Fork 任务已创建", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/tasks\/43$/);
});
