import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { seedState } from "./transport-fixtures";
import { API_ORIGIN as origin } from "./e2e-env";

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

/** 各工作区桶里的标签 id（空桶不列），以及右侧停靠区当前显示的面板。 */
async function state(page: Page) {
  return page.evaluate(async () => {
    const modulePath = "/lib/workbench/store.ts";
    const dockModulePath = "/lib/layout/dock-store.ts";
    const { useWorkbenchStore } = await import(modulePath);
    const { useDockStore } = await import(dockModulePath);
    const sessions: Record<number, { tabs: { id: string }[] }> = useWorkbenchStore.getState().sessions;
    const tabs = Object.fromEntries(Object.entries(sessions)
      .filter(([, session]) => session.tabs.length > 0)
      .map(([id, session]) => [id, session.tabs.map((tab) => tab.id)]));
    return { tabs, panel: useDockStore.getState().panel as string | null };
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
  // 侧栏开关钉在左上角：收起、展开都在同一位置，状态由 aria-expanded 表达；收起即整个隐藏。
  // 它总是所在那一行拖动区的后代（展开时在侧栏头部，收起时在页面头部）：Electron 按 DOM 顺序合并拖动区，
  // 做成跨列浮层会被后面的拖动区盖掉而点不动，而 Playwright 不认拖动区，只能这样守住结构。
  const sidebarToggle = page.getByRole("button", { name: "切换侧栏", exact: true });
  const dragRowOf = () => sidebarToggle.evaluate((element) => {
    const row = element.closest(".app-drag-region");
    return row?.closest("aside") ? "sidebar" : row?.tagName === "HEADER" ? "page-header" : "none";
  });
  const togglePosition = (await sidebarToggle.boundingBox())!;
  await expect(sidebarToggle).toHaveAttribute("aria-expanded", "true");
  expect(await dragRowOf()).toBe("sidebar");
  await sidebarToggle.click();
  await expect(sidebarToggle).toHaveAttribute("aria-expanded", "false");
  expect(await dragRowOf()).toBe("page-header");
  await expect.poll(async () => (await page.locator("aside").first().boundingBox())?.width ?? 0).toBe(0);
  expect(await sidebarToggle.boundingBox()).toEqual(togglePosition);
  await expect(resize).toHaveAttribute("aria-valuenow", "736");
  const grip = (await resize.boundingBox())!;
  await page.mouse.move(grip.x + 3, grip.y + 100);
  await page.mouse.down();
  await page.mouse.move(0, grip.y + 100);
  await page.mouse.up();
  await sidebarToggle.click();
  await expect(sidebarToggle).toHaveAttribute("aria-expanded", "true");
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
  expect(await state(page)).toEqual({ tabs: { 7: ["agent-team-config-draft:team", "agent-config-draft:draft"] }, panel: "workbench" });
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
  expect((await state(page)).tabs[7]).toHaveLength(1);
});

test("新对话页选择其他工作区和命令面板跳转都不确认，各工作区的工作台互不影响", async ({ page }) => {
  await page.goto("/");
  await seedDrafts(page);
  await page.getByRole("treeitem", { name: "工作区8", exact: true }).click();
  await expect(switchDialog(page)).toHaveCount(0);
  await expect(page.getByRole("treeitem", { name: "工作区8", exact: true })).toHaveAttribute("aria-current", "true");
  await expect(page.getByRole("tab", { name: /未保存Agent/ })).toHaveCount(0);
  // 文件是独立面板，不进工作台的标签桶。
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  await expect(page.getByRole("complementary", { name: "文件" })).toBeVisible();

  await page.getByRole("navigation", { name: "当前位置" }).click();
  const modifier = await page.evaluate(() => /mac/i.test(navigator.userAgent) ? "Meta" : "Control");
  await page.keyboard.press(`${modifier}+k`);
  await page.getByRole("dialog", { name: "命令面板" }).getByRole("combobox").fill("任务100");
  await page.getByRole("option", { name: /任务100/ }).click();
  await expect(page).toHaveURL(/\/tasks\/100$/);
  await expect(page.getByRole("complementary", { name: "文件" })).toBeVisible();
  await page.getByRole("button", { name: /工作台，1 个标签，点击打开/ }).click();
  await expect(page.getByRole("tab", { name: /未保存Agent/ })).toBeVisible();
  // 停靠区是全局的一个选择：工作区 7 的工作台有标签，回到它时仍是刚才选中的文件面板，切到工作台后草稿原样还在。
  expect(await state(page)).toEqual({ tabs: { 7: ["agent-config-draft:draft"] }, panel: "workbench" });
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
