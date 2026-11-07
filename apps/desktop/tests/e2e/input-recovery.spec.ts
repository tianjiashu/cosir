import { expect, test } from "@playwright/test";
import { API_ORIGIN as origin } from "./e2e-env";
import { completedState, seedState } from "./transport-fixtures";

const TASK = 42;
const OTHER = 43;

test.beforeEach(async ({ request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: TASK, title: "甲任务" } });
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: OTHER, title: "乙任务" } });
});

test("发送在途时切走 Task，被拒后内容回到原 Task 的输入框并有提示", async ({ page, request }) => {
  await seedState(request, TASK, {
    title: "甲任务",
    messages: [
      { id: "u-1", role: "user", runId: 1, source: null, outcome: null, parts: [{ type: "text", text: "第一个问题", status: "completed" }] },
      { id: "assistant-u-1", role: "assistant", runId: 1, source: null, outcome: null, parts: [{ type: "text", text: "正在处理", status: "running" }] },
    ],
    activity: { modelActivity: null, runId: 1, status: "suspended", endReason: null, error: null, usage: null, contextWindowTotal: null, launch: { modelConfigId: 2, reasoningEffort: null, disabledToolGroups: [] }, startedAt: "2026-10-27T00:00:00.000Z" },
  });
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route(`**/tasks/${TASK}/messages`, async (route) => {
    await gate;
    await route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: { error: { code: "TEST_REJECTED", message: "测试拒绝" } } }) });
  });
  await page.goto(`/tasks/${TASK}`);
  await expect(page.getByText("正在处理", { exact: true })).toBeVisible();
  await page.getByLabel("消息输入").fill("这是一段写了很久的消息");
  const sent = page.waitForRequest((req) => req.method() === "POST" && new URL(req.url()).pathname === `/tasks/${TASK}/messages`);
  await page.getByRole("button", { name: "加入队列", exact: true }).click();
  await sent;
  await page.getByRole("treeitem").filter({ hasText: "乙任务" }).click();
  await expect(page).toHaveURL(/\/tasks\/43$/);

  release();
  // 用户已离开：不抢占当前页面，用 toast 说明内容回到了哪里。
  await expect(page.getByText("发送失败，内容已放回「甲任务」的输入框")).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);

  await page.getByRole("treeitem").filter({ hasText: "甲任务" }).click();
  await expect(page).toHaveURL(/\/tasks\/42$/);
  await expect(page.getByLabel("消息输入")).toContainText("这是一段写了很久的消息");
});

for (const switchAway of [false, true]) test(`缺失文件提示的「查找同名文件」${switchAway ? "切走 Task 后" : "留在原处时"}点击生效`, async ({ page, request }) => {
  await page.route("**/workspaces/*/files/**", async (route) => {
    const operation = new URL(route.request().url()).pathname.split("/").at(-1);
    if (operation === "info") return route.fulfill({ status: 404, json: { detail: { code: "FILE_NOT_FOUND", message: "文件不存在" } } });
    if (operation === "visibility") return route.fulfill({ json: { ignored_paths: [], revision: "empty" } });
    if (operation === "directory") return route.fulfill({ json: { path: "", entries: [], next_cursor: null, total: 0 } });
    return route.fallback();
  });
  await seedState(request, TASK, completedState(1, { id: "user-1", parts: [{ type: "text", text: "跑一下" }] }, [{ type: "text", text: "旧文件 [gone](src/gone.ts)。" }]));
  await page.goto(`/tasks/${TASK}`);
  await page.getByRole("link", { name: "gone" }).click();
  await expect(page.getByText("工作区里没有 src/gone.ts")).toBeVisible();
  if (switchAway) {
    await page.getByRole("treeitem").filter({ hasText: "乙任务" }).click();
    await expect(page).toHaveURL(/\/tasks\/43$/);
  }
  await page.getByRole("button", { name: "查找同名文件" }).click();
  await expect(page.getByTestId("workspace-files-surface").getByRole("textbox", { name: "搜索工作区文件名" })).toHaveValue("gone.ts");
});
