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
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId: 42, title: "布局隔离" } });
  const state = completedState(1, { id: "u", parts: [{ type: "text", text: "检查布局" }] }, [
    { type: "text", text: "布局隔离回复", status: "completed", finalReply: true },
  ]);
  state.title = "布局隔离";
  await seedState(request, 42, state);
  // 侧栏只在有内容时出现；给一条已结束的后台工作，下面才测得到它在固定与浮层之间切换。
  await request.post(`${origin}/__test__/seed-background-tasks`, { data: { sessionId: 42, foregroundWaits: [], entries: [
    { toolCallId: "call-done", kind: "terminal_command", label: "npm run lint", status: "completed", startedAt: "2026-01-01T00:00:00Z", endedAt: "2026-01-01T00:00:30Z" },
  ] } });
  await page.goto("/");
  await expect(page.getByRole("treeitem").filter({ hasText: "布局隔离" })).toBeVisible();
  // 首次挂载会话前包装已有 memo 组件，只在测试浏览器中计数，不给生产代码增加探针接口。
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
  await expect.poll(() => openedSubscriptionCount(page, { sessionId: 42 })).toBe(1);
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
  expect(await openedSubscriptionCount(page, { sessionId: 42 })).toBe(1);
});

test("跨工作区切换后，目录失效推送只重读对应工作区，Fork 使用当前会话和消息", async ({ page, request }) => {
  const session = (id: number, workspaceId: number) => ({
    session_id: id, workspace_id: workspaceId, title: `会话${id}`, session_type: "user",
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  });
  const first = session(42, 7);
  const current = session(43, 8);
  const forked = session(44, 8);
  let forkCreated = false;
  const refreshed: number[] = [];
  await page.route(`${origin}/workspaces`, route => route.fulfill({ json: [7, 8].map(id => ({
    workspace_id: id, name: `工作区${id}`, root_path: `/workspace/${id}`,
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  })) }));
  for (const id of [7, 8]) await page.route(`${origin}/workspaces/${id}/sessions`, route => {
    refreshed.push(id);
    return route.fulfill({ json: id === 7 ? [first] : forkCreated ? [current, forked] : [current] });
  });
  for (const item of [first, current, forked]) {
    await page.route(`${origin}/sessions/${item.session_id}`, route => route.fulfill({ json: item }));
    const state = completedState(1, { id: `${item.session_id}`, parts: [{ type: "text", text: item.title }] }, [
      { type: "text", text: `回复${item.session_id}`, status: "completed", finalReply: true },
    ]);
    state.title = item.title;
    await seedState(request, item.session_id, state);
  }
  await page.goto("/sessions/42");
  await expect(page.getByText("回复42", { exact: true })).toBeVisible();
  await page.getByRole("treeitem").filter({ hasText: "会话43" }).click();
  await expect(page.getByText("回复43", { exact: true })).toBeVisible();
  await expect.poll(() => openedSubscriptionCount(page, { sessionId: 43 })).toBe(1);
  refreshed.length = 0;
  // 后端写入标题后推送工作区 8 的目录失效：侧栏只重读这个工作区，不按页面上的 snapshot 标题推断。
  current.title = "当前会话的新标题";
  await request.post(`${origin}/__test__/publish-catalog`, { data: { workspaceIds: [8] } });
  await expect(page.getByRole("treeitem").filter({ hasText: current.title })).toBeVisible();
  expect(refreshed).toEqual([8]);

  await page.setViewportSize({ width: 1500, height: 760 });
  const calls: { url: string; body: unknown }[] = [];
  await page.route(`${origin}/sessions/*/fork`, route => {
    calls.push({ url: route.request().url(), body: route.request().postDataJSON() });
    forkCreated = true;
    return route.fulfill({ json: forked });
  });
  await page.getByRole("button", { name: "从这里分叉", exact: true }).last().click();
  await expect(page).toHaveURL(/\/sessions\/44$/);
  expect(calls).toEqual([{ url: `${origin}/sessions/43/fork`, body: { replyMessageId: "assistant-turn-1" } }]);
});
