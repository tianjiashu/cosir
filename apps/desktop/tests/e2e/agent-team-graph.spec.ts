import { expect, test } from "@playwright/test";

test.beforeEach(async ({ request }) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
});

test("Agent 工具栏拖拽、画布内编辑、多 END 落点与全屏保存", async ({ page }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  const reviewer = {
    agent_id: "reviewer",
    role: "child",
    description: "审查代码并给出修改建议",
    system_prompt: "",
    allowed_tool_groups: ["文件"],
    max_steps: 20,
    model_config_id: null,
    model_settings: {},
    source: "user_file",
    path: ".cosir/agents/reviewer.json",
    editable: true,
    deletable: true,
    validation_status: "valid",
    validation_error: null,
    file_name: "reviewer.json",
  };
  let configurations: Record<string, unknown>[] = [];
  let savedConfiguration: Record<string, unknown> | null = null;

  await page.route("**/configuration/agents", (route) => route.fulfill({ json: [reviewer] }));
  await page.route("**/configuration/agent-teams", async (route) => {
    if (route.request().method() === "POST") {
      savedConfiguration = (route.request().postDataJSON() as { configuration: Record<string, unknown> }).configuration;
      configurations = [savedConfiguration];
      await route.fulfill({ json: savedConfiguration });
      return;
    }
    await route.fulfill({ json: configurations });
  });

  await page.addInitScript(() => window.localStorage.clear());
  await page.goto("/settings");
  await page.getByRole("button", { name: "Agent Team 配置", exact: true }).click();
  await page.getByRole("button", { name: "新建 Team" }).click();
  await page.getByRole("button", { name: "画布" }).click();
  await expect(page.getByLabel("Team ID")).toBeHidden();
  await expect(page.getByLabel("入口节点")).toBeHidden();
  await expect(page.getByRole("button", { name: "保存配置" })).toBeHidden();
  await page.getByRole("button", { name: "Team 设置" }).click();
  const teamSettings = page.getByRole("dialog");
  await teamSettings.getByLabel("Team ID").fill("review_flow");
  await teamSettings.getByLabel("Team 名称").fill("Review Flow");
  await teamSettings.getByLabel("用途说明").fill("审查并修复代码");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();

  const paletteItem = page.getByLabel("可拖拽子 Agent reviewer");
  await paletteItem.dragTo(page.locator(".react-flow__pane"), { targetPosition: { x: 330, y: 260 }, steps: 12 });
  const paletteLogEvents = await page.evaluate(() => (window as Window & { __cosirFrontendLogs?: { event: string }[] }).__cosirFrontendLogs?.map((entry) => entry.event) ?? []);
  expect(paletteLogEvents).toContain("agent_team_graph_palette_drag_started");
  expect(paletteLogEvents).toContain("agent_team_graph_palette_drop_completed");
  await expect(page.locator('.react-flow__node[data-id="node_1"]')).toBeVisible();
  await expect(page.getByRole("textbox", { name: "节点名称" })).toHaveValue("reviewer");
  await page.getByRole("textbox", { name: "节点名称" }).fill("代码审查");

  await page.getByRole("button", { name: "全屏画布" }).click();
  await expect(page.getByRole("region", { name: "Agent Team 画布编辑器" })).toHaveClass(/fixed/);
  await page.getByRole("button", { name: "Team 设置" }).click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(page.getByRole("region", { name: "Agent Team 画布编辑器" })).toHaveClass(/fixed/);

  await page.getByRole("button", { name: "添加 reviewer" }).click();
  await expect(page.locator('.react-flow__node[data-id="node_2"]')).toBeVisible();
  await page.locator('.react-flow__node[data-id="node_2"]').click();
  await page.getByRole("textbox", { name: "节点 ID" }).fill("node_1");
  await page.getByRole("textbox", { name: "节点 ID" }).press("Tab");
  await expect(page.getByRole("alert")).toHaveText("该节点 ID 已被使用");
  await expect(page.getByRole("alert")).toHaveCount(1);
  await expect(page.locator('.react-flow__node[data-id="node_2"]')).toBeVisible();
  await page.getByRole("button", { name: "添加 END" }).click();
  await expect(page.locator('.react-flow__node[data-id^="@@canvas-end-"]')).toHaveCount(2);

  await page.locator('.react-flow__node[data-id="node_1"]').click();
  await page.getByLabel("新增业务状态").fill("needs_review");
  await page.getByRole("button", { name: "添加业务状态" }).click();
  await page.getByRole("button", { name: "关闭节点属性" }).click();
  await expect(page.getByRole("region", { name: "节点属性编辑器" })).toHaveCount(0);

  const doneEdge = page.getByRole("group", { name: "Edge from node_1 to @@canvas-end-1" });
  const reviewOutput = page.locator('.react-flow__node[data-id="node_1"] .react-flow__handle[data-handleid="status:needs_review"]');
  const secondEndInput = page.locator('.react-flow__node[data-id="@@canvas-end-2"] .react-flow__handle[data-handleid="input"]');
  await doneEdge.click();
  await page.keyboard.press("Delete");
  await expect(page.locator(".react-flow__edge")).toHaveCount(1);
  await page.keyboard.press("Control+z");
  await expect(page.locator(".react-flow__edge")).toHaveCount(2);
  await doneEdge.click();
  await page.getByLabel("转移属性").getByLabel("目标节点").selectOption("node_2");
  await expect(page.getByRole("group", { name: "Edge from node_1 to node_2" })).toBeVisible();
  await page.getByRole("button", { name: "关闭转移属性" }).click();
  await reviewOutput.dragTo(secondEndInput, { steps: 12 });
  await expect(page.getByRole("group", { name: "Edge from node_1 to @@canvas-end-2" })).toBeVisible();
  await expect(page.locator(".react-flow__edge")).toHaveCount(3);
  await page.getByRole("button", { name: "关闭转移属性" }).click();
  const nodePositionBeforeDrag = await page.evaluate(() => {
    const entry = Object.entries(window.localStorage).find(([key]) => key.includes("agent-team-graph"));
    return entry ? (JSON.parse(entry[1]) as { positions?: Record<string, { x: number; y: number }> }).positions?.node_2 : undefined;
  });
  await page.locator('.react-flow__node[data-id="node_2"]').dragTo(page.locator(".react-flow__pane"), { targetPosition: { x: 700, y: 400 }, steps: 12 });
  await expect(page.locator('.react-flow__node[data-id="node_2"]')).toBeVisible();
  await expect(page.locator(".react-flow__node")).toHaveCount(4);
  for (const node of await page.locator(".react-flow__node").all()) await expect(node).toBeVisible();
  const nodePositionAfterDrag = await page.evaluate(() => {
    const entry = Object.entries(window.localStorage).find(([key]) => key.includes("agent-team-graph"));
    return entry ? (JSON.parse(entry[1]) as { positions?: Record<string, { x: number; y: number }> }).positions?.node_2 : undefined;
  });
  expect(nodePositionAfterDrag).toBeDefined();
  expect(nodePositionAfterDrag).not.toEqual(nodePositionBeforeDrag);
  const nodeDragLogEvents = await page.evaluate(() => (window as Window & { __cosirFrontendLogs?: { event: string }[] }).__cosirFrontendLogs?.map((entry) => entry.event) ?? []);
  expect(nodeDragLogEvents).toContain("agent_team_graph_node_drag_started");
  expect(nodeDragLogEvents).toContain("agent_team_graph_node_drag_completed");
  await page.locator('.react-flow__node[data-id="node_2"]').click();
  await page.keyboard.press("Delete");
  await expect(page.locator(".react-flow__node")).toHaveCount(3);
  await expect(page.locator(".react-flow__edge")).toHaveCount(1);
  await page.keyboard.press("Control+z");
  await expect(page.locator(".react-flow__node")).toHaveCount(4);
  await expect(page.locator(".react-flow__edge")).toHaveCount(3);
  await page.keyboard.press("Control+Shift+z");
  await expect(page.locator(".react-flow__node")).toHaveCount(3);
  await page.getByRole("button", { name: "撤销" }).click();
  await expect(page.locator(".react-flow__node")).toHaveCount(4);
  await expect(page.locator(".react-flow__edge")).toHaveCount(3);
  expect(pageErrors).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("region", { name: "Agent Team 画布编辑器" })).not.toHaveClass(/fixed/);

  await page.getByRole("button", { name: "表单" }).click();
  await expect(page.getByLabel("Team ID")).toHaveValue("review_flow");
  await expect(page.getByLabel("Team 名称")).toHaveValue("Review Flow");
  await expect(page.getByLabel("用途说明")).toHaveValue("审查并修复代码");
  await expect(page.getByRole("region", { name: "Agent Team 画布编辑器" })).toBeHidden();
  await expect(page.getByLabel("入口节点")).toHaveValue("node_1");
  await page.getByRole("button", { name: "画布" }).click();
  await expect(page.getByLabel("Team ID")).toBeHidden();
  await expect(page.getByLabel("入口节点")).toBeHidden();
  await expect(page.locator('.react-flow__node[data-id^="@@canvas-end-"]')).toHaveCount(2);
  await expect(page.locator(".react-flow__edge")).toHaveCount(3);
  await expect(page.getByRole("button", { name: "撤销" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "重做" })).toBeEnabled();
  await page.getByRole("button", { name: "重做" }).click();
  await expect(page.locator('.react-flow__node[data-id="node_2"]')).toHaveCount(0);
  await page.getByRole("button", { name: "撤销" }).click();
  await expect(page.locator('.react-flow__node[data-id="node_2"]')).toBeVisible();
  const canvasLayout = await page.evaluate(() => {
    const stored = Object.entries(window.localStorage).find(([key]) => key.includes("agent-team-graph"));
    return stored ? JSON.parse(stored[1]) as { endNodeIds: string[]; endTargets: Record<string, string> } : null;
  });
  expect(canvasLayout?.endNodeIds).toHaveLength(2);
  expect(canvasLayout?.endTargets["node_1::needs_review"]).toBe("@@canvas-end-2");
  await page.getByRole("button", { name: "保存", exact: true }).click();

  await expect.poll(() => savedConfiguration).not.toBeNull();
  expect(savedConfiguration).toMatchObject({
    team_id: "review_flow",
    start_node_id: "node_1",
    nodes: [
      { node_id: "node_1", name: "代码审查", agent_id: "reviewer", statuses: ["done", "needs_review"] },
      { node_id: "node_2", name: "reviewer", agent_id: "reviewer", statuses: ["done"] },
    ],
    transitions: [
      { from_node_id: "node_1", status: "done", target_node_id: "node_2" },
      { from_node_id: "node_2", status: "done", target_node_id: "END" },
      { from_node_id: "node_1", status: "needs_review", target_node_id: "END" },
    ],
  });
  await expect.poll(() => page.evaluate(() => Object.keys(window.localStorage).some((key) => key.includes("agent-team-graph") && key.endsWith(":review_flow")))).toBe(true);
  expect(await page.evaluate(() => Object.keys(window.localStorage).some((key) => key.includes("agent-team-graph") && key.endsWith(":new")))).toBe(false);
  await expect(page.getByRole("status")).toHaveText("配置更改成功");
});
