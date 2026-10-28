import { expect, test, type Page } from "@playwright/test";
import { teamConfiguration } from "../unit/team-editor-fixture";
const team = { ...teamConfiguration, scope: "system" as const, max_runs: 10 };
const node = (page: Page, index: number) => page.locator(`.react-flow__node[data-id="@@agent-node-${index}"]`);
const transform = (page: Page, index: number) => node(page, index).evaluate(element => element.style.transform);
async function openSettings(page: Page) {
  await page.route("**/configuration/agents", route => route.fulfill({ json: [] }));
  await page.goto("/settings");
  await page.getByRole("navigation", { name: "系统配置分类" }).getByRole("link", { name: "Agent Team", exact: true }).click();
}
test.beforeEach(async ({ request }) => { await request.post("http://127.0.0.1:8000/__test__/reset"); });

test("表单删除不转移节点位置，跨视图撤销还原布局与保存基线", async ({ page }) => {
  await page.route("**/configuration/agent-teams", route => route.fulfill({ json: [team] }));
  await openSettings(page);
  await page.getByRole("button", { name: "编辑 Review", exact: true }).click();
  await page.getByRole("button", { name: "画布", exact: true }).click();
  await expect(node(page, 1)).toBeVisible();
  const before = await transform(page, 1);
  await page.getByRole("button", { name: "返回 Team 编辑" }).click();
  await page.getByRole("button", { name: "删除节点 Review 1", exact: true }).click();
  await page.getByRole("button", { name: "画布", exact: true }).click();
  await expect(node(page, 0)).toHaveCount(0);
  expect(await transform(page, 1)).toBe(before);
  await page.getByRole("button", { name: "撤销", exact: true }).click();
  await expect(node(page, 0)).toBeVisible();
  expect(await transform(page, 1)).toBe(before);
  // 画布默认全屏，先回到表单再返回列表。
  await page.getByRole("button", { name: "返回 Team 编辑" }).click();
  await page.getByRole("button", { name: "返回 Team 列表", exact: true }).click();
  await expect(page.getByRole("button", { name: "新建 Team" })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("保存期间的新编辑保留，离开需要确认，第二次保存使用已创建身份", async ({ page }) => {
  let submitted: typeof team | null = null;
  let release!: () => void;
  const released = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/configuration/agent-teams", async route => {
    if (route.request().method() === "POST") {
      submitted = route.request().postDataJSON().configuration;
      await released;
      await route.fulfill({ json: submitted });
    } else await route.fulfill({ json: [] });
  });
  let updatedName: string | undefined;
  await page.route("**/configuration/agent-teams/new_team", route => {
    const configuration = route.request().postDataJSON().configuration;
    updatedName = configuration.name;
    return route.fulfill({ json: configuration });
  });
  await openSettings(page);
  await page.getByRole("button", { name: "新建 Team" }).click();
  await page.getByLabel("Team ID").fill("new_team");
  await page.getByLabel("Team 名称").fill("提交版本");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect.poll(() => submitted?.name).toBe("提交版本");
  await page.getByLabel("Team 名称").fill("后来修改");
  release();
  await expect(page.getByText("已保存提交版本，仍有新修改待保存", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Team 名称")).toHaveValue("后来修改");
  await page.getByRole("button", { name: "返回 Team 列表" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.getByLabel("Team 名称")).toHaveValue("后来修改");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect.poll(() => updatedName).toBe("后来修改");
  await page.getByRole("button", { name: "返回 Team 列表" }).click();
  await expect(page.getByRole("button", { name: "新建 Team" })).toBeVisible();
});

test("工作台切换标签保留草稿和历史，非活动 Team 草稿仍参与离开守卫", async ({ page, request }) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 42, title: "会话回归" } });
  await page.route("**/configuration/agents", route => route.fulfill({ json: [] }));
  await page.goto("/tasks/42");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  // 通过真实工作台装配入口创建工具草稿；后续操作及断言均使用页面。
  await page.evaluate(async draft => {
    const modulePath = "/lib/workbench/store.ts";
    const { useWorkbenchStore } = await import(/* @vite-ignore */ modulePath);
    const store = useWorkbenchStore.getState();
    store.setPanelWidth(900);
    store.openAgentTeamConfigurationDraftTab({ workspaceId: 7, taskId: 42, toolCallId: "session-test", title: "Team 会话", draft });
  }, teamConfiguration);
  await page.getByLabel("Team 名称").fill("保留的新名字");
  await page.getByRole("button", { name: "画布", exact: true }).click();
  await expect(node(page, 0)).toBeVisible();
  await page.evaluate(async () => {
    const modulePath = "/lib/workbench/store.ts";
    const { useWorkbenchStore } = await import(/* @vite-ignore */ modulePath);
    useWorkbenchStore.getState().openAgentTab({ workspaceId: 7, taskId: 42, title: "另一个标签", role: null });
  });
  await expect(page.getByRole("region", { name: "Agent Team 画布编辑器" })).toHaveCount(0);
  await page.getByRole("button", { name: "收起工作台" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("tab", { name: /Team 会话/ }).getByRole("button", { name: "Team 会话", exact: true }).click();
  await expect(page.getByRole("region", { name: "Agent Team 画布编辑器" })).toBeVisible();
  await page.getByRole("button", { name: "返回 Team 编辑" }).click();
  await expect(page.getByLabel("Team 名称")).toHaveValue("保留的新名字");
  await page.getByRole("button", { name: "撤销", exact: true }).click();
  await expect(page.getByLabel("Team 名称")).toHaveValue("Review");
});

test("表单逐字修改节点 ID 途中与其他节点重名，转移仍留在原节点", async ({ page }) => {
  await page.route("**/configuration/agent-teams", route => route.fulfill({ json: [team] }));
  await openSettings(page);
  await page.getByRole("button", { name: "编辑 Review", exact: true }).click();
  const fix = page.getByRole("article", { name: "节点 Fix" });
  const review = page.getByRole("article", { name: "节点 Review" });
  const nodeId = fix.getByLabel("节点 ID");
  await nodeId.click();
  await nodeId.press("ControlOrMeta+A");
  // 逐字输入会经过与 Review 相同的 "reviewer"；节点 ID 失焦才提交，中间态不能改写转移归属。
  await nodeId.pressSequentially("reviewer2");
  await nodeId.press("Tab");
  await expect(nodeId).toHaveValue("reviewer2");
  await expect(fix.getByLabel("目标节点")).toHaveValue("reviewer");
  await expect(review.getByLabel("触发状态")).toHaveCount(2);
  await expect(review.getByLabel("目标节点").nth(1)).toHaveValue("reviewer2");
});

test("结构检查结果标到大纲、画布节点与连线，点问题跳到对应节点", async ({ page }) => {
  await page.route("**/configuration/agent-teams", route => route.fulfill({ json: [team] }));
  const checked: unknown[] = [];
  await page.route("**/configuration/agent-teams/graph-issues", route => {
    checked.push(route.request().postDataJSON());
    return route.fulfill({ json: { issues: [
      { code: "dead_end_node", message: "节点“fixer”之后无法走到 END。", node_id: "fixer", status: null, transition_index: null },
      { code: "unknown_target", message: "节点“reviewer”的状态“needs_changes”指向了不存在的节点，请改为有效节点或 END。", node_id: "reviewer", status: "needs_changes", transition_index: 1 },
    ] } });
  });
  await openSettings(page);
  await page.getByRole("button", { name: "编辑 Review", exact: true }).click();

  // 只提交参与图校验的字段，名称、颜色等不进入检查请求。
  await expect.poll(() => checked.length).toBeGreaterThan(0);
  expect(JSON.stringify(checked[0])).not.toContain("color");
  await expect(page.getByRole("button", { name: "2 个问题" })).toBeVisible();
  await expect(page.getByRole("article", { name: "节点 Fix" })).toContainText("之后无法走到 END");
  await expect(page.getByRole("article", { name: "节点 Review" })).toContainText("指向了不存在的节点");

  await page.getByRole("button", { name: "画布", exact: true }).click();
  await expect(node(page, 1).getByLabel("1 个结构问题")).toBeVisible();
  await page.getByRole("region", { name: "Agent Team 画布编辑器" }).getByRole("button", { name: "2 个问题" }).click();
  await page.getByRole("button", { name: "节点“fixer”之后无法走到 END。" }).click();
  await expect(page.getByRole("region", { name: "节点属性编辑器" })).toContainText("之后无法走到 END");
});

test("整理布局把有回路的 Team 从 START 到 END 自左向右排开，并可一步撤销", async ({ page }) => {
  await page.route("**/configuration/agent-teams", route => route.fulfill({ json: [team] }));
  await openSettings(page);
  await page.getByRole("button", { name: "编辑 Review", exact: true }).click();
  await page.getByRole("button", { name: "画布", exact: true }).click();
  await expect(node(page, 1)).toBeVisible();
  const end = page.locator('.react-flow__node[data-id="@@canvas-end-1"]');
  const flowX = (locator: typeof end) => locator.evaluate(element => new DOMMatrixReadOnly(getComputedStyle(element).transform).m41);
  const before = await transform(page, 1);

  await page.getByRole("button", { name: "整理布局" }).click();
  // fixture：Review（入口）→ Fix → 回到 Review，Review 的 done → END。
  await expect.poll(async () => (await flowX(node(page, 0))) < (await flowX(node(page, 1)))).toBe(true);
  await expect.poll(async () => (await flowX(node(page, 1))) < (await flowX(end))).toBe(true);
  const logs = await page.evaluate(() => (window as Window & { __cosirFrontendLogs?: { event: string }[] }).__cosirFrontendLogs?.map(entry => entry.event) ?? []);
  expect(logs).toContain("agent_team_graph_auto_layout_applied");

  await page.getByRole("button", { name: "撤销", exact: true }).click();
  await expect.poll(() => transform(page, 1)).toBe(before);
});
