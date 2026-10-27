import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { seedState } from "./transport-fixtures";

const origin = "http://127.0.0.1:8000";
const task = (task_id: number, workspace_id: number) => ({
  task_id, workspace_id, title: `任务${task_id}`, task_type: "user", execution_status: null,
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
    await seedState(request, id, { messages: [], activity: null });
  }
}

/** 只装配前端草稿夹具，不发起模型运行或配置写入；交互仍经过真实布局、路由和确认框。 */
async function seedDrafts(page: Page, team = false) {
  await expect(page.getByRole("button", { name: "浏览工作区文件" })).toBeVisible();
  await page.evaluate(async (withTeam) => {
    const modulePath = "/lib/workbench/store.ts";
    const { useWorkbenchStore } = await import(modulePath);
    const store = useWorkbenchStore.getState();
    if (withTeam) store.openAgentTeamConfigurationDraftTab({
      workspaceId: 7, taskId: 100, toolCallId: "team", title: "未保存Team",
      draft: { team_id: "team", name: "未保存Team", description: "", max_runs: 8,
        start_node_id: "review", nodes: [{ node_id: "review", name: "审查", agent_id: "reviewer", statuses: [{ name: "done", color: "#16a34a" }] }],
        transitions: [{ from_node_id: "review", status: "done", target_node_id: "END" }], scope: "workspace" },
    });
    store.openAgentConfigurationDraftTab({
      workspaceId: 7, taskId: 100, toolCallId: "draft", title: "未保存Agent", scope: "workspace",
      draft: { agent_id: "draft", role: "审查", description: "保留这份编辑", system_prompt: "检查代码",
        allowed_tool_groups: [], max_steps: 100, model_config_id: null, model_settings: {} },
    });
  }, team);
  await expect(page.getByRole("tab", { name: /未保存Agent/ })).toBeVisible();
}

async function state(page: Page) {
  return page.evaluate(async () => {
    const modulePath = "/lib/workbench/store.ts";
    const { useWorkbenchStore } = await import(modulePath);
    const state = useWorkbenchStore.getState();
    return { workspaceId: state.workspaceId, tabs: state.tabs.map((tab: { id: string }) => tab.id), panelOpen: state.panelOpen };
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
  await expect(resize).toHaveAttribute("aria-valuenow", "752");
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

test("两处收起入口保留编辑，未激活Team和收起的Agent都参与切换确认", async ({ page }) => {
  await page.goto("/tasks/100");
  await seedDrafts(page, true);
  await page.getByRole("textbox", { name: "描述", exact: true }).fill("新的草稿内容");
  await page.getByRole("button", { name: "收起工作台", exact: true }).click();
  await expect(page.getByRole("complementary", { name: "工作台" })).toHaveCount(0);
  await expect(switchDialog(page)).toHaveCount(0);
  await page.getByRole("button", { name: /工作台，2 个标签，点击打开/ }).click();
  await expect(page.getByRole("textbox", { name: "描述", exact: true })).toHaveValue("新的草稿内容");
  await page.getByRole("button", { name: /工作台，2 个标签，点击收起/ }).click();
  await expect(switchDialog(page)).toHaveCount(0);
  await taskRow(page, 200).click();
  await expect(switchDialog(page)).toContainText("未保存Team");
  await expect(switchDialog(page)).toContainText("未保存Agent");
  await switchDialog(page).getByRole("button", { name: "取消", exact: true }).click();
  await expect(page).toHaveURL(/\/tasks\/100$/);
  expect((await state(page)).tabs).toHaveLength(2);
  await taskRow(page, 200).click();
  await switchDialog(page).getByRole("button", { name: "放弃草稿并切换" }).click();
  await expect(page).toHaveURL(/\/tasks\/200$/);
  await expect.poll(() => state(page)).toEqual({ workspaceId: 8, tabs: [], panelOpen: false });
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
  expect((await state(page)).tabs).toHaveLength(1);
});

test("新对话页直接选择工作区，取消不改变选择，确认销毁草稿", async ({ page }) => {
  await page.goto("/");
  await seedDrafts(page);
  await page.getByRole("treeitem", { name: "工作区8", exact: true }).click();
  await expect(switchDialog(page)).toBeVisible();
  await switchDialog(page).getByRole("button", { name: "取消", exact: true }).click();
  expect((await state(page)).workspaceId).toBe(7);
  await expect(page.getByRole("treeitem", { name: "工作区7", exact: true })).toHaveAttribute("aria-current", "true");
  await page.getByRole("treeitem", { name: "工作区8", exact: true }).click();
  await switchDialog(page).getByRole("button", { name: "放弃草稿并切换" }).click();
  await expect.poll(() => state(page)).toEqual({ workspaceId: 8, tabs: [], panelOpen: false });
});

for (const [unknownId, crossWorkspace] of [[998, false], [999, true]] as const) {
  test(`历史导航到目录未收录任务${unknownId}，先解析归属再决定丢弃`, async ({ page }) => {
    await page.goto(`/tasks/${unknownId}`);
    await taskRow(page, 100).click();
    await seedDrafts(page);
    await page.goBack();
    if (crossWorkspace) {
      await expect(switchDialog(page)).toBeVisible();
      await switchDialog(page).getByRole("button", { name: "取消", exact: true }).click();
      await expect(page).toHaveURL(/\/tasks\/100$/);
      expect((await state(page)).tabs).toHaveLength(1);
      await page.goBack();
      await switchDialog(page).getByRole("button", { name: "放弃草稿并切换" }).click();
      await expect.poll(() => state(page)).toEqual({ workspaceId: 8, tabs: [], panelOpen: false });
    } else {
      await expect(page).toHaveURL(/\/tasks\/998$/);
      await expect(switchDialog(page)).toHaveCount(0);
      expect((await state(page)).tabs).toHaveLength(1);
    }
  });
}

test("下拉选择工作区取消时不提前写入最近工作区偏好", async ({ page }) => {
  await page.goto("/");
  await seedDrafts(page);
  const choose = async () => {
    await page.getByRole("button", { name: "工作区7", exact: true }).click();
    await page.getByRole("option", { name: /工作区8/ }).click();
  };
  await choose();
  await expect(switchDialog(page)).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("cosir:last-workspace-id"))).toBe("7");
  await switchDialog(page).getByRole("button", { name: "取消", exact: true }).click();
  expect((await state(page)).workspaceId).toBe(7);
  expect(await page.evaluate(() => localStorage.getItem("cosir:last-workspace-id"))).toBe("7");
  await choose();
  await switchDialog(page).getByRole("button", { name: "放弃草稿并切换" }).click();
  await expect.poll(() => state(page)).toEqual({ workspaceId: 8, tabs: [], panelOpen: false });
  expect(await page.evaluate(() => localStorage.getItem("cosir:last-workspace-id"))).toBe("8");
});

test("任务保活时选择后续工作区不销毁草稿，新对话和命令面板真正离开时确认", async ({ page }) => {
  await page.goto("/tasks/100");
  await seedDrafts(page);
  await page.getByRole("treeitem", { name: "工作区8", exact: true }).click();
  await expect(page).toHaveURL(/\/tasks\/100$/);
  await expect(switchDialog(page)).toHaveCount(0);
  expect((await state(page)).workspaceId).toBe(7);
  await page.getByRole("button", { name: "新对话", exact: true }).click();
  await expect(switchDialog(page)).toBeVisible();
  await switchDialog(page).getByRole("button", { name: "取消", exact: true }).click();
  await expect(page).toHaveURL(/\/tasks\/100$/);
  expect((await state(page)).tabs).toHaveLength(1);
  await expect(switchDialog(page)).toHaveCount(0);
  await page.getByRole("navigation", { name: "当前位置" }).click();
  const modifier = await page.evaluate(() => /mac/i.test(navigator.userAgent) ? "Meta" : "Control");
  await page.keyboard.press(`${modifier}+k`);
  await page.getByRole("dialog", { name: "命令面板" }).getByRole("combobox").fill("任务200");
  await page.getByRole("option", { name: /任务200/ }).click();
  await expect(page.getByRole("dialog", { name: "切换工作区并放弃草稿？" })).toBeVisible();
  await switchDialog(page).getByRole("button", { name: "放弃草稿并切换" }).click();
  await expect(page).toHaveURL(/\/tasks\/200$/);
  await expect.poll(() => state(page)).toEqual({ workspaceId: 8, tabs: [], panelOpen: false });
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
    const store = (await import(modulePath)).useWorkbenchStore.getState();
    const agent = store.tabs.find((tab: {kind: string}) => tab.kind === "agent-configuration-draft");
    const team = store.tabs.find((tab: {kind: string}) => tab.kind === "agent-team-configuration-draft");
    store.setDraftDirty(agent.id, false);
    team.session.store.setState({ dirty: false });
  });
  await expect(agent.locator(".sr-only")).toHaveCount(0);
  await expect(team.locator(".sr-only")).toHaveCount(0);
  await page.evaluate(async () => {
    const modulePath = "/lib/workbench/store.ts";
    const store = (await import(modulePath)).useWorkbenchStore.getState();
    store.tabs.find((tab: {kind: string}) => tab.kind === "agent-team-configuration-draft").session.updateConfiguration({ description: "新的后台编辑" });
  });
  await expect(team.locator(".sr-only")).toHaveText("未保存");
  await expect(team).toHaveAttribute("aria-selected", "false");
});
