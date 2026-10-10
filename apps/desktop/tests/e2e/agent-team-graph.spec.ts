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
  await expect(page.locator('.react-flow__node[data-id="@@agent-node-0"]')).toBeVisible();
  await expect(page.locator(".react-flow__edge")).toHaveCount(0);
  const nodeNameInput = page.getByRole("textbox", { name: "节点名称" });
  await expect(nodeNameInput).toHaveValue("reviewer");
  await nodeNameInput.click();
  expect(await nodeNameInput.evaluate((element) => document.activeElement === element)).toBe(true);
  await nodeNameInput.press("Control+A");
  await nodeNameInput.pressSequentially("Code Review");
  await expect(nodeNameInput).toHaveValue("Code Review");
  const fieldLogEvents = await page.evaluate(() => (window as Window & { __cosirFrontendLogs?: { event: string; data?: { field?: string } }[] }).__cosirFrontendLogs?.filter((entry) => entry.event.startsWith("agent_team_graph_node_field_")).map((entry) => entry.event) ?? []);
  expect(fieldLogEvents).toContain("agent_team_graph_node_field_focused");
  expect(fieldLogEvents).toContain("agent_team_graph_node_field_changed");
  await nodeNameInput.press("Backspace");
  await expect(nodeNameInput).toHaveValue("Code Revie");
  await expect(page.locator(".react-flow__node")).toHaveCount(2);
  await nodeNameInput.click();
  expect(await nodeNameInput.evaluate((element) => document.activeElement === element)).toBe(true);
  await nodeNameInput.press("Control+A");
  await nodeNameInput.pressSequentially("代码审查");
  await expect(nodeNameInput).toHaveValue("代码审查");

  await page.getByRole("button", { name: "全屏画布" }).click();
  await expect(page.getByRole("region", { name: "Agent Team 画布编辑器" })).toHaveClass(/fixed/);
  await page.getByRole("button", { name: "Team 设置" }).click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(page.getByRole("region", { name: "Agent Team 画布编辑器" })).toHaveClass(/fixed/);

  await page.getByRole("button", { name: "添加 reviewer" }).click();
  await expect(page.locator('.react-flow__node[data-id="@@agent-node-1"]')).toBeVisible();
  await expect(page.locator(".react-flow__edge")).toHaveCount(0);
  await page.locator('.react-flow__node[data-id="@@agent-node-1"]').click();
  await page.getByRole("textbox", { name: "节点 ID" }).fill("node_1");
  await page.getByRole("textbox", { name: "节点 ID" }).press("Tab");
  await expect(page.getByRole("textbox", { name: "节点 ID" })).toHaveValue("node_1");
  await expect(page.locator('.react-flow__node[data-id="@@agent-node-1"]')).toBeVisible();
  await page.getByRole("textbox", { name: "节点 ID" }).fill("node_2");
  await page.getByRole("textbox", { name: "节点 ID" }).press("Tab");
  await expect(page.locator('.react-flow__node[data-id="@@agent-node-1"]')).toBeVisible();
  const endPaletteItem = page.getByLabel("可拖拽结束出口 END");
  await expect(endPaletteItem).toBeVisible();
  await expect(page.getByRole("button", { name: "添加 END" })).toHaveCount(0);
  await endPaletteItem.dragTo(page.locator(".react-flow__pane"), { targetPosition: { x: 620, y: 420 }, steps: 12 });
  await expect(page.locator('.react-flow__node[data-id^="@@canvas-end-"]')).toHaveCount(2);
  const endPaletteLogEvents = await page.evaluate(() => (window as Window & { __cosirFrontendLogs?: { event: string }[] }).__cosirFrontendLogs?.map((entry) => entry.event) ?? []);
  expect(endPaletteLogEvents).toContain("agent_team_graph_end_palette_drag_started");
  expect(endPaletteLogEvents).toContain("agent_team_graph_end_palette_drop_completed");

  await page.locator('.react-flow__node[data-id="@@agent-node-0"]').click();
  await page.getByLabel("新增业务状态").fill("needs_review");
  await page.getByRole("button", { name: "添加业务状态" }).click();
  await page.getByRole("button", { name: "关闭节点属性" }).click();
  await expect(page.getByRole("region", { name: "节点属性编辑器" })).toHaveCount(0);

  const doneOutput = page.locator('.react-flow__node[data-id="@@agent-node-0"] .react-flow__handle[data-handleid="status:done"]');
  const node2Input = page.locator('.react-flow__node[data-id="@@agent-node-1"] .react-flow__handle[data-handleid="input"]');
  const node2Output = page.locator('.react-flow__node[data-id="@@agent-node-1"] .react-flow__handle[data-handleid="status:done"]');
  const firstEndInput = page.locator('.react-flow__node[data-id="@@canvas-end-1"] .react-flow__handle[data-handleid="input"]');
  const reviewOutput = page.locator('.react-flow__node[data-id="@@agent-node-0"] .react-flow__handle[data-handleid="status:needs_review"]');
  const secondEndInput = page.locator('.react-flow__node[data-id="@@canvas-end-2"] .react-flow__handle[data-handleid="input"]');
  await doneOutput.dragTo(node2Input, { steps: 12 });
  await node2Output.dragTo(firstEndInput, { steps: 12 });
  await reviewOutput.dragTo(secondEndInput, { steps: 12 });
  await expect(page.locator(".react-flow__edge")).toHaveCount(3);
  const doneEdge = page.locator('.react-flow__edge[data-id="transition-0"]');
  await expect(doneEdge).toHaveAttribute("aria-label", "Edge from @@agent-node-0 to @@agent-node-1");
  await doneEdge.click({ force: true });
  await expect(page.getByLabel("转移属性")).toHaveCount(0);
  await page.keyboard.press("Delete");
  await expect(page.locator(".react-flow__edge")).toHaveCount(2);
  await page.keyboard.press("Control+z");
  await expect(page.locator(".react-flow__edge")).toHaveCount(3);
  const needsReviewEdge = page.locator('.react-flow__edge[data-id="transition-2"]');
  await needsReviewEdge.click({ force: true });
  const reconnectHandleBounds = await needsReviewEdge.locator(".react-flow__edgeupdater-target").boundingBox();
  const reconnectTargetBounds = await node2Input.boundingBox();
  expect(reconnectHandleBounds).not.toBeNull();
  expect(reconnectTargetBounds).not.toBeNull();
  await page.mouse.move(reconnectHandleBounds!.x + reconnectHandleBounds!.width / 2, reconnectHandleBounds!.y + reconnectHandleBounds!.height / 2);
  await page.mouse.down();
  await page.mouse.move(reconnectTargetBounds!.x + reconnectTargetBounds!.width / 2, reconnectTargetBounds!.y + reconnectTargetBounds!.height / 2, { steps: 16 });
  await page.mouse.up();
  await expect(needsReviewEdge).toHaveAttribute("aria-label", "Edge from @@agent-node-0 to @@agent-node-1");
  const reconnectionLogs = await page.evaluate(() => (window as Window & { __cosirFrontendLogs?: { event: string; data?: { oldEdgeId?: string; target?: string } }[] }).__cosirFrontendLogs?.filter((entry) => entry.event === "agent_team_graph_reconnect_committed") ?? []);
  expect(reconnectionLogs).toContainEqual(expect.objectContaining({ data: expect.objectContaining({ oldEdgeId: "transition-2", target: "@@agent-node-1" }) }));
  await page.locator(".react-flow__pane").click({ position: { x: 30, y: 30 } });
  const nodePositionBeforeDrag = await page.evaluate(() => {
    const entry = Object.entries(window.localStorage).find(([key]) => key.includes("agent-team-graph"));
    return entry ? (JSON.parse(entry[1]) as { positions?: Record<string, { x: number; y: number }> }).positions?.["@@agent-node-1"] : undefined;
  });
  const node2 = page.locator('.react-flow__node[data-id="@@agent-node-1"]');
  const node2Bounds = await node2.boundingBox();
  expect(node2Bounds).not.toBeNull();
  await page.mouse.move(node2Bounds!.x + node2Bounds!.width / 2, node2Bounds!.y + node2Bounds!.height / 2);
  await page.mouse.down();
  await page.mouse.move(node2Bounds!.x + node2Bounds!.width / 2 + 160, node2Bounds!.y + node2Bounds!.height / 2 + 110, { steps: 12 });
  await page.mouse.up();
  await expect(page.locator('.react-flow__node[data-id="@@agent-node-1"]')).toBeVisible();
  await expect(page.locator(".react-flow__node")).toHaveCount(4);
  for (const node of await page.locator(".react-flow__node").all()) await expect(node).toBeVisible();
  await expect.poll(() => page.evaluate(() => {
    const entry = Object.entries(window.localStorage).find(([key]) => key.includes("agent-team-graph"));
    return entry ? (JSON.parse(entry[1]) as { positions?: Record<string, { x: number; y: number }> }).positions?.["@@agent-node-1"] : undefined;
  })).not.toEqual(nodePositionBeforeDrag);
  const nodePositionAfterDrag = await page.evaluate(() => {
    const entry = Object.entries(window.localStorage).find(([key]) => key.includes("agent-team-graph"));
    return entry ? (JSON.parse(entry[1]) as { positions?: Record<string, { x: number; y: number }> }).positions?.["@@agent-node-1"] : undefined;
  });
  expect(nodePositionAfterDrag).toBeDefined();
  const nodeDragLogEvents = await page.evaluate(() => (window as Window & { __cosirFrontendLogs?: { event: string }[] }).__cosirFrontendLogs?.map((entry) => entry.event) ?? []);
  expect(nodeDragLogEvents).toContain("agent_team_graph_node_drag_started");
  expect(nodeDragLogEvents).toContain("agent_team_graph_node_drag_completed");
  await page.locator('.react-flow__node[data-id="@@agent-node-1"]').click();
  await page.keyboard.press("Delete");
  await expect(page.locator(".react-flow__node")).toHaveCount(3);
  await expect(page.locator(".react-flow__edge")).toHaveCount(0);
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
  await expect(page.locator('.react-flow__node[data-id="@@agent-node-1"]')).toHaveCount(0);
  await page.getByRole("button", { name: "撤销" }).click();
  await expect(page.locator('.react-flow__node[data-id="@@agent-node-1"]')).toBeVisible();
  const canvasLayout = await page.evaluate(() => {
    const stored = Object.entries(window.localStorage).find(([key]) => key.includes("agent-team-graph"));
    return stored ? JSON.parse(stored[1]) as { endNodeIds: string[]; endTargets: Record<string, string> } : null;
  });
  expect(canvasLayout?.endNodeIds).toHaveLength(2);
  expect(canvasLayout?.endTargets["node_2::done"]).toBe("@@canvas-end-1");
  await page.getByRole("button", { name: "保存", exact: true }).click();

  await expect.poll(() => savedConfiguration).not.toBeNull();
  const persistedConfiguration = savedConfiguration as unknown as Record<string, unknown>;
  expect(persistedConfiguration).toMatchObject({
    team_id: "review_flow",
    start_node_id: "node_1",
    nodes: [
    { node_id: "node_1", name: "代码审查", agent_id: "reviewer", statuses: ["done", "needs_review"] },
      { node_id: "node_2", name: "reviewer", agent_id: "reviewer", statuses: ["done"] },
    ],
  });
  expect(persistedConfiguration.transitions).toEqual(expect.arrayContaining([
    { from_node_id: "node_1", status: "done", target_node_id: "node_2" },
    { from_node_id: "node_2", status: "done", target_node_id: "END" },
    { from_node_id: "node_1", status: "needs_review", target_node_id: "node_2" },
  ]));
  await expect.poll(() => page.evaluate(() => Object.keys(window.localStorage).some((key) => key.includes("agent-team-graph") && key.endsWith(":review_flow")))).toBe(true);
  expect(await page.evaluate(() => Object.keys(window.localStorage).some((key) => key.includes("agent-team-graph") && key.endsWith(":new")))).toBe(false);
  const saveSuccess = page.getByRole("dialog");
  await expect(saveSuccess.getByText("保存成功")).toBeVisible();
  await expect(saveSuccess.getByText("Agent Team 配置已保存。")).toBeVisible();
  await saveSuccess.getByRole("button", { name: "继续编辑" }).click();
  await expect(page.getByLabel("Team ID")).toHaveValue("review_flow");
  await expect(page.getByLabel("Team ID")).toBeDisabled();
  await page.getByRole("button", { name: "返回 Team 列表" }).click();
  await expect(page.getByRole("heading", { name: "Review Flow" })).toBeVisible();
});

test("Agent Team 保存把校验交给后端并在失败时弹窗保留草稿", async ({ page }) => {
  const reviewer = {
    agent_id: "reviewer",
    role: "child",
    description: "审查代码",
    system_prompt: "",
    allowed_tool_groups: [],
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
  let submittedConfiguration: Record<string, unknown> | null = null;

  await page.route("**/configuration/agents", (route) => route.fulfill({ json: [reviewer] }));
  await page.route("**/configuration/agent-teams", async (route) => {
    if (route.request().method() === "POST") {
      submittedConfiguration = (route.request().postDataJSON() as { configuration: Record<string, unknown> }).configuration;
      await route.fulfill({ status: 400, json: { detail: "Team ID 只能包含字母、数字、下划线和连字符" } });
      return;
    }
    await route.fulfill({ json: [] });
  });

  await page.addInitScript(() => window.localStorage.clear());
  await page.goto("/settings");
  await page.getByRole("button", { name: "Agent Team 配置", exact: true }).click();
  await page.getByRole("button", { name: "新建 Team" }).click();
  await page.getByLabel("Team ID").fill("invalid id");
  await page.getByLabel("Team 名称").fill("Validation test");
  await page.getByLabel("用途说明").fill("验证后端保存校验");
  await page.getByRole("button", { name: "保存配置" }).click();

  const saveFailure = page.getByRole("dialog");
  await expect(saveFailure.getByText("保存失败")).toBeVisible();
  await expect(saveFailure.getByText("Team ID 只能包含字母、数字、下划线和连字符")).toBeVisible();
  expect(submittedConfiguration).toMatchObject({ team_id: "invalid id", name: "Validation test" });
  await saveFailure.getByRole("button", { name: "返回修改" }).click();
  await expect(page.getByLabel("Team ID")).toHaveValue("invalid id");
  await expect(page.getByLabel("Team 名称")).toHaveValue("Validation test");
});

test("添加转移不再弹出前端校验提示，未完成草稿提交给后端", async ({ page }) => {
  const reviewer = {
    agent_id: "reviewer",
    role: "child",
    description: "审查代码",
    system_prompt: "",
    allowed_tool_groups: [],
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
  let submittedConfiguration: Record<string, unknown> | null = null;

  await page.route("**/configuration/agents", (route) => route.fulfill({ json: [reviewer] }));
  await page.route("**/configuration/agent-teams", async (route) => {
    if (route.request().method() === "POST") {
      submittedConfiguration = (route.request().postDataJSON() as { configuration: Record<string, unknown> }).configuration;
      await route.fulfill({ status: 400, json: { detail: "后端校验：请至少添加一个 Team 节点" } });
      return;
    }
    await route.fulfill({ json: [] });
  });

  await page.addInitScript(() => window.localStorage.clear());
  await page.goto("/settings");
  await page.getByRole("button", { name: "Agent Team 配置", exact: true }).click();
  await page.getByRole("button", { name: "新建 Team" }).click();
  await page.getByRole("button", { name: "添加转移" }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.locator("[data-team-entry-card]")).toHaveCount(1);
  await page.getByLabel("Team ID").fill("draft_team");
  await page.getByLabel("Team 名称").fill("Draft Team");
  await page.getByLabel("用途说明").fill("提交未完成草稿");
  await page.getByRole("button", { name: "保存配置" }).click();

  const saveFailure = page.getByRole("dialog");
  await expect(saveFailure.getByText("后端校验：请至少添加一个 Team 节点")).toBeVisible();
  expect((submittedConfiguration as unknown as { transitions: unknown[] }).transitions)
    .toEqual([{ from_node_id: "", status: "", target_node_id: "END" }]);
});
