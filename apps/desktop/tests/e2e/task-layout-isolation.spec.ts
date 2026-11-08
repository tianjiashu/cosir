import { expect, test, type Page } from "@playwright/test";

import { API_ORIGIN as origin } from "./e2e-env";
import { openedSubscriptionCount } from "./frontend-logs";
import { completedState, seedState } from "./transport-fixtures";

type RenderCounts = { session: number; thread: number };

/** 等尺寸观察和 React 提交经过浏览器帧，不依赖任意毫秒的静默期。 */
async function settleLayout(page: Page) {
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
}

test.beforeEach(async ({ page, request }) => {
  await request.post(`${origin}/__test__/reset`);
  await page.setViewportSize({ width: 1600, height: 900 });
});

test("窗口宽高与面板开关不重新执行会话和线程，也不重连订阅", async ({ page, request }) => {
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: 42, title: "布局隔离" } });
  const state = completedState(1, { id: "u", parts: [{ type: "text", text: "检查布局" }] }, [
    { type: "text", text: "布局隔离回复", status: "completed", finalReply: true },
  ]);
  state.title = "布局隔离";
  await seedState(request, 42, state);
  await page.goto("/");
  await expect(page.getByRole("treeitem").filter({ hasText: "布局隔离" })).toBeVisible();
  // 首次挂载任务前包装已有 memo 组件，只在测试浏览器中计数，不给生产代码增加探针接口。
  await page.evaluate(async () => {
    const sessionPath = "/components/conversation-runtime/runtime/assistant-runtime-session.tsx";
    const threadPath = "/components/conversation/thread/thread.aui.tsx";
    const { AssistantRuntimeSession } = await import(/* @vite-ignore */ sessionPath);
    const { Thread } = await import(/* @vite-ignore */ threadPath);
    const counts = { session: 0, thread: 0 };
    Object.assign(window, { layoutRenderCounts: counts });
    for (const [component, name] of [[AssistantRuntimeSession, "session"], [Thread, "thread"]] as const) {
      const render = component.type;
      component.type = (...args: unknown[]) => { counts[name]++; return render(...args); };
    }
  });
  await page.getByRole("treeitem").filter({ hasText: "布局隔离" }).click();
  await expect(page.getByText("布局隔离回复", { exact: true })).toBeVisible();
  // 工具目录加载完成也是合法会话更新，先等它完成再测纯布局变化。
  await page.getByRole("button", { name: "工具", exact: true }).click();
  await expect(page.getByRole("switch").first()).toBeEnabled();
  await page.keyboard.press("Escape");
  await expect.poll(() => openedSubscriptionCount(page, { taskId: 42 })).toBe(1);
  await settleLayout(page);
  const counts = () => page.evaluate(() => (window as unknown as { layoutRenderCounts: RenderCounts }).layoutRenderCounts);
  expect((await counts()).session).toBeGreaterThan(0);
  expect((await counts()).thread).toBeGreaterThan(0);
  await page.evaluate(() => Object.assign((window as unknown as { layoutRenderCounts: RenderCounts }).layoutRenderCounts, { session: 0, thread: 0 }));

  for (const height of [760, 900]) {
    await page.setViewportSize({ width: 1600, height });
    await settleLayout(page);
    expect(await counts()).toEqual({ session: 0, thread: 0 });
  }
  await page.setViewportSize({ width: 1500, height: 900 });
  await settleLayout(page);
  expect(await counts()).toEqual({ session: 0, thread: 0 });
  await expect(page.getByRole("complementary", { name: "会话侧栏" })).toHaveAttribute("data-conversation-aside", "docked");
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  await page.getByRole("button", { name: /^会话侧栏/ }).click();
  await expect(page.getByRole("complementary", { name: "会话侧栏" })).toHaveAttribute("data-conversation-aside", "floating");
  await page.getByRole("button", { name: "收起文件", exact: true }).click();
  await settleLayout(page);
  expect(await counts()).toEqual({ session: 0, thread: 0 });
  expect(await openedSubscriptionCount(page, { taskId: 42 })).toBe(1);
});

test("跨工作区切换后，状态通知刷新当前目录，Fork 使用当前任务和消息", async ({ page, request }) => {
  const task = (id: number, workspaceId: number) => ({
    task_id: id, workspace_id: workspaceId, title: `任务${id}`, task_type: "user",
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  });
  const first = task(42, 7);
  const current = task(43, 8);
  const forked = task(44, 8);
  let forkCreated = false;
  const refreshed: number[] = [];
  await page.route(`${origin}/workspaces`, route => route.fulfill({ json: [7, 8].map(id => ({
    workspace_id: id, name: `工作区${id}`, root_path: `/workspace/${id}`,
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  })) }));
  for (const id of [7, 8]) await page.route(`${origin}/workspaces/${id}/tasks`, route => {
    refreshed.push(id);
    return route.fulfill({ json: id === 7 ? [first] : forkCreated ? [current, forked] : [current] });
  });
  const states = new Map<number, ReturnType<typeof completedState>>();
  for (const item of [first, current, forked]) {
    await page.route(`${origin}/tasks/${item.task_id}`, route => route.fulfill({ json: item }));
    const state = completedState(1, { id: `${item.task_id}`, parts: [{ type: "text", text: item.title }] }, [
      { type: "text", text: `回复${item.task_id}`, status: "completed", finalReply: true },
    ]);
    state.title = item.title;
    states.set(item.task_id, state);
    await seedState(request, item.task_id, state);
  }
  await page.goto("/tasks/42");
  await expect(page.getByText("回复42", { exact: true })).toBeVisible();
  await page.getByRole("treeitem").filter({ hasText: "任务43" }).click();
  await expect(page.getByText("回复43", { exact: true })).toBeVisible();
  await expect.poll(() => openedSubscriptionCount(page, { taskId: 43 })).toBe(1);
  refreshed.length = 0;
  current.title = "当前任务的新标题";
  const state = states.get(43)!;
  state.title = current.title;
  await seedState(request, 43, state);
  await expect(page.getByRole("treeitem").filter({ hasText: current.title })).toBeVisible();
  expect(refreshed).toEqual([8]);

  await page.setViewportSize({ width: 1500, height: 760 });
  const calls: { url: string; body: unknown }[] = [];
  await page.route(`${origin}/tasks/*/fork`, route => {
    calls.push({ url: route.request().url(), body: route.request().postDataJSON() });
    forkCreated = true;
    return route.fulfill({ json: forked });
  });
  await page.getByRole("button", { name: "从这里分叉", exact: true }).last().click();
  await expect(page).toHaveURL(/\/tasks\/44$/);
  expect(calls).toEqual([{ url: `${origin}/tasks/43/fork`, body: { replyMessageId: "assistant-turn-1" } }]);
});
