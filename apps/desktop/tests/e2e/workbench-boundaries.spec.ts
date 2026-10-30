import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { seedState } from "./transport-fixtures";

const origin = "http://127.0.0.1:8000";
const task = (task_id: number, workspace_id: number) => ({
  task_id, workspace_id, title: `任务${task_id}`, task_type: "user",
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
});

async function fixture(page: Page, request: APIRequestContext) {
  await request.post(`${origin}/__test__/reset`);
  await page.addInitScript(() => {
    localStorage.clear();
    localStorage.setItem("cosir:last-workspace-id", "7");
  });
  await page.route(`${origin}/workspaces`, route => route.fulfill({ json: [7, 8].map(id => ({
    workspace_id: id, name: `工作区${id}`, root_path: `/workspace/${id}`,
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  })) }));
  await page.route(`${origin}/workspaces/7/tasks`, route => route.fulfill({ json: [task(100, 7), task(101, 7)] }));
  await page.route(`${origin}/workspaces/8/tasks`, route => route.fulfill({ json: [task(200, 8)] }));
  for (const [id, workspace] of [[100, 7], [101, 7], [200, 8], [999, 8], [998, 7]]) {
    await page.route(`${origin}/tasks/${id}`, route => route.fulfill({ json: task(id, workspace) }));
    // 对话 state 与 Task 长订阅由模拟后端持有：订阅需要常驻连接，路由夹具无法保持。
    await seedState(request, id, { title: "测试对话", messages: [], activity: null });
  }
}

/** 只装配前端草稿夹具，不发起模型运行或配置写入；交互仍经过真实布局、路由和确认框。 */
async function seedDrafts(page: Page, team = false) {
  await expect(page.getByRole("button", { name: "浏览工作区文件" })).toBeVisible();
  await page.evaluate(async (withTeam) => {
    const modulePath = "/lib/workbench/store.ts";
    const { useWorkbenchStore } = await import(modulePath);
    const store = useWorkbenchStore.getState();
    if (withTeam) store.openAgentTeamConfigurationDraft(7, {
      taskId: 100, toolCallId: "team",
      draft: { team_id: "team", name: "未保存Team", description: "", max_runs: 8,
        start_node_id: "review", nodes: [{ node_id: "review", name: "审查", agent_id: "reviewer", statuses: [{ name: "done", color: "#16a34a" }] }],
        transitions: [{ from_node_id: "review", status: "done", target_node_id: "END" }], scope: "workspace" },
    });
    store.openAgentConfigurationDraft(7, {
      taskId: 100, toolCallId: "draft",
      draft: { agent_id: "未保存Agent", role: "审查", description: "保留这份编辑", system_prompt: "检查代码" },
    });
  }, team);
  await expect(page.getByRole("tab", { name: /未保存Agent/ })).toBeVisible();
}

/** 各工作区桶里的标签 id 与面板开合。 */
async function state(page: Page) {
  return page.evaluate(async () => {
    const modulePath = "/lib/workbench/store.ts";
    const { useWorkbenchStore } = await import(modulePath);
    const sessions: Record<number, { tabs: { id: string }[]; panelOpen: boolean }> = useWorkbenchStore.getState().sessions;
    return Object.fromEntries(Object.entries(sessions).map(([id, session]) => [id, { tabs: session.tabs.map((tab) => tab.id), panelOpen: session.panelOpen }]));
  });
}

const switchDialog = (page: Page) => page.getByRole("dialog");
const taskRow = (page: Page, id: number) => page.getByRole("treeitem", { name: `任务${id}`, exact: true });

test.beforeEach(async ({ page, request }) => fixture(page, request));

test("窗口和侧栏变化限制实际宽度，End 和鼠标拖动均保留主区", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/tasks/100");
  await seedDrafts(page);
  const resize = page.getByRole("separator", { name: "调整工作台宽度" });
  await resize.focus();
  await page.keyboard.press("End");
  for (const width of [1280, 1025, 960, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(async () => (await page.getByRole("main").boundingBox())!.width).toBeGreaterThanOrEqual(399);
    const panel = (await page.getByRole("complementary", { name: "工作台" }).boundingBox())!;
    expect(panel.x + panel.width).toBeLessThanOrEqual(width + 1);
  }
  await page.getByRole("button", { name: "收起侧栏", exact: true }).click();
  await expect(resize).toHaveAttribute("aria-valuenow", "736");
  const grip = (await resize.boundingBox())!;
  await page.mouse.move(grip.x + 3, grip.y + 100);
  await page.mouse.down();
  await page.mouse.move(0, grip.y + 100);
  await page.mouse.up();
  await page.getByRole("button", { name: "展开侧栏", exact: true }).click();
  await expect.poll(async () => (await page.locator("aside").first().boundingBox())!.width).toBe(288);
  await expect.poll(async () => (await page.getByRole("main").boundingBox())!.width).toBeGreaterThanOrEqual(399);
  await page.screenshot({ path: testInfo.outputPath("bounded-workbench.png") });
});

test("收起入口保留编辑；切到其他工作区的任务不确认、不丢草稿，切回原样恢复", async ({ page }) => {
  await page.goto("/tasks/100");
  await seedDrafts(page, true);
  await page.getByRole("textbox", { name: "描述", exact: true }).fill("新的草稿内容");
  await page.getByRole("button", { name: "收起工作台", exact: true }).click();
  await expect(page.getByRole("complementary", { name: "工作台" })).toHaveCount(0);
  await expect(switchDialog(page)).toHaveCount(0);
  await page.getByRole("button", { name: /工作台，2 个标签，点击打开/ }).click();
  await expect(page.getByRole("textbox", { name: "描述", exact: true })).toHaveValue("新的草稿内容");

  await taskRow(page, 200).click();
  await expect(page).toHaveURL(/\/tasks\/200$/);
  await expect(switchDialog(page)).toHaveCount(0);
  await expect(page.getByRole("complementary", { name: "工作台" })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: /未保存Agent/ })).toHaveCount(0);

  await taskRow(page, 100).click();
  await expect(page).toHaveURL(/\/tasks\/100$/);
  await expect(page.getByRole("tab", { name: /未保存Team/ })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "描述", exact: true })).toHaveValue("新的草稿内容");
  expect((await state(page))[7]).toEqual({ tabs: ["agent-team-config-draft:team", "agent-config-draft:draft"], panelOpen: true });
});

test("同工作区任务和配置页保活草稿，真正关闭仍确认", async ({ page }) => {
  await page.goto("/tasks/100");
  await seedDrafts(page);
  await taskRow(page, 101).click();
  await expect(page).toHaveURL(/\/tasks\/101$/);
  await expect(switchDialog(page)).toHaveCount(0);
  await page.getByRole("button", { name: "系统配置", exact: true }).click();
  await expect(page).toHaveURL(/\/settings\//);
  await expect(page.getByRole("tab", { name: /未保存Agent/ })).toBeVisible();
  await expect(switchDialog(page)).toHaveCount(0);
  await page.getByRole("button", { name: "关闭 未保存Agent", exact: true }).click();
  await expect(switchDialog(page)).toContainText("关闭配置草稿");
  await switchDialog(page).getByRole("button", { name: "取消", exact: true }).click();
  expect((await state(page))[7].tabs).toHaveLength(1);
});

test("新对话页选择其他工作区和命令面板跳转都不确认，各工作区的工作台互不影响", async ({ page }) => {
  await page.goto("/");
  await seedDrafts(page);
  await page.getByRole("treeitem", { name: "工作区8", exact: true }).click();
  await expect(switchDialog(page)).toHaveCount(0);
  await expect(page.getByRole("treeitem", { name: "工作区8", exact: true })).toHaveAttribute("aria-current", "true");
  await expect(page.getByRole("tab", { name: /未保存Agent/ })).toHaveCount(0);
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  await expect(page.getByRole("tab", { name: /文件/ })).toBeVisible();

  await page.getByRole("navigation", { name: "当前位置" }).click();
  const modifier = await page.evaluate(() => /mac/i.test(navigator.userAgent) ? "Meta" : "Control");
  await page.keyboard.press(`${modifier}+k`);
  await page.getByRole("dialog", { name: "命令面板" }).getByRole("combobox").fill("任务100");
  await page.getByRole("option", { name: /任务100/ }).click();
  await expect(page).toHaveURL(/\/tasks\/100$/);
  await expect(page.getByRole("tab", { name: /未保存Agent/ })).toBeVisible();
  expect(await state(page)).toEqual({
    7: { tabs: ["agent-config-draft:draft"], panelOpen: true },
    8: { tabs: ["workspace-files"], panelOpen: true },
  });
});

test("切换工作台快捷键收起与展开当前工作区的工作台", async ({ page }) => {
  await page.goto("/tasks/100");
  await seedDrafts(page);
  const modifier = await page.evaluate(() => /mac/i.test(navigator.userAgent) ? "Meta" : "Control");
  await page.keyboard.press(`${modifier}+Alt+KeyB`);
  await expect(page.getByRole("complementary", { name: "工作台" })).toHaveCount(0);
  await page.keyboard.press(`${modifier}+Alt+KeyB`);
  await expect(page.getByRole("complementary", { name: "工作台" })).toBeVisible();
});

test("Agent 与未激活 Team 标签直接反映草稿未保存状态", async ({ page }) => {
  await page.goto("/tasks/100");
  await seedDrafts(page, true);
  const agent = page.getByRole("tab", { name: /未保存Agent/ });
  const team = page.getByRole("tab", { name: /未保存Team/ });
  await expect(agent.locator(".sr-only")).toHaveText("未保存");
  await expect(team.locator(".sr-only")).toHaveText("未保存");
  await expect(team).toHaveAttribute("aria-selected", "false");
  await page.evaluate(async () => {
    const modulePath = "/lib/workbench/store.ts";
    const tabs = (await import(modulePath)).useWorkbenchStore.getState().sessions[7].tabs;
    const agent = tabs.find((tab: {kind: string}) => tab.kind === "agent-configuration-draft");
    const team = tabs.find((tab: {kind: string}) => tab.kind === "agent-team-configuration-draft");
    agent.session.store.setState({ dirty: false });
    team.session.store.setState({ dirty: false });
  });
  await expect(agent.locator(".sr-only")).toHaveCount(0);
  await expect(team.locator(".sr-only")).toHaveCount(0);
  await page.evaluate(async () => {
    const modulePath = "/lib/workbench/store.ts";
    const tabs = (await import(modulePath)).useWorkbenchStore.getState().sessions[7].tabs;
    tabs.find((tab: {kind: string}) => tab.kind === "agent-team-configuration-draft").session.updateConfiguration({ description: "新的后台编辑" });
  });
  await expect(team.locator(".sr-only")).toHaveText("未保存");
  await expect(team).toHaveAttribute("aria-selected", "false");
});
