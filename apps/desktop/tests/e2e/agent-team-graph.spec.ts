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
  await page.getByRole("navigation", { name: "系统配置分类" }).getByRole("link", { name: "Agent Team", exact: true }).click();
  await page.getByRole("button", { name: "新建 Team" }).click();
  await page.getByRole("button", { name: "画布" }).click();
  await expect(page.getByRole("button", { name: "保存配置" })).toBeHidden();
  // 新建 Team 进入画布时属性面板默认展示 Team 信息，取代原先的「Team 设置」对话框。
  const teamPanel = page.getByRole("region", { name: "Team 属性" });
  await teamPanel.getByLabel("Team ID").fill("review_flow");
  await teamPanel.getByLabel("Team 名称").fill("Review Flow");
  await teamPanel.getByLabel("用途说明").fill("审查并修复代码");
  await expect(page.getByRole("button", { name: "Team 属性", exact: true })).toContainText("Review Flow");
  await page.getByRole("button", { name: "关闭 Team 属性" }).click();
  await expect(teamPanel).toHaveCount(0);

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
  await nodeNameInput.press("ControlOrMeta+A");
  await nodeNameInput.pressSequentially("Code Review");
  await expect(nodeNameInput).toHaveValue("Code Review");
  const fieldLogEvents = await page.evaluate(() => (window as Window & { __cosirFrontendLogs?: { event: string; data?: { field?: string } }[] }).__cosirFrontendLogs?.filter((entry) => entry.event.startsWith("agent_team_node_field_")).map((entry) => entry.event) ?? []);
  expect(fieldLogEvents).toContain("agent_team_node_field_focused");
  expect(fieldLogEvents).toContain("agent_team_node_field_changed");
  await nodeNameInput.press("Backspace");
  await expect(nodeNameInput).toHaveValue("Code Revie");
  await expect(page.locator(".react-flow__node")).toHaveCount(2);
  await nodeNameInput.click();
  expect(await nodeNameInput.evaluate((element) => document.activeElement === element)).toBe(true);
  await nodeNameInput.press("ControlOrMeta+A");
  await nodeNameInput.pressSequentially("代码审查");
  await expect(nodeNameInput).toHaveValue("代码审查");

  // 切到画布即默认全屏。
  await expect(page.getByRole("region", { name: "Agent Team 画布编辑器" })).toHaveClass(/fixed/);
  // 输入框里的 Escape 不退出全屏；点标题回到 Team 属性。
  await page.getByRole("textbox", { name: "节点名称" }).press("Escape");
  await expect(page.getByRole("region", { name: "Agent Team 画布编辑器" })).toHaveClass(/fixed/);
  await page.getByRole("button", { name: "Team 属性", exact: true }).click();
  await expect(page.getByRole("region", { name: "Team 属性" })).toBeVisible();
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

  // 添加节点时画布可能已平移，先按 F 让全部节点回到视野内。
  await page.locator(".react-flow__pane").click({ position: { x: 10, y: 10 } });
  await page.keyboard.press("f");
  await page.locator('.react-flow__node[data-id="@@agent-node-0"]').click();
  const nodeEditor = page.getByRole("region", { name: "节点属性编辑器" });
  await nodeEditor.getByRole("button", { name: "添加状态" }).click();
  await nodeEditor.getByLabel("添加业务状态").fill("needs_review");
  await nodeEditor.getByLabel("添加业务状态").press("Enter");
  await expect(nodeEditor.getByRole("button", { name: "移除状态 needs_review" })).toBeVisible();
  await page.getByRole("button", { name: "关闭节点属性" }).click();
  await expect(page.getByRole("region", { name: "节点属性编辑器" })).toHaveCount(0);

  const doneOutput = page.locator('.react-flow__node[data-id="@@agent-node-0"] .react-flow__handle[aria-label="done 状态出口，拖出以创建连线"]');
  const node2Input = page.locator('.react-flow__node[data-id="@@agent-node-1"] .react-flow__handle[data-handleid="input"]');
  const node2Output = page.locator('.react-flow__node[data-id="@@agent-node-1"] .react-flow__handle[aria-label="done 状态出口，拖出以创建连线"]');
  const firstEndInput = page.locator('.react-flow__node[data-id="@@canvas-end-1"] .react-flow__handle[data-handleid="input"]');
  const reviewOutput = page.locator('.react-flow__node[data-id="@@agent-node-0"] .react-flow__handle[aria-label="needs_review 状态出口，拖出以创建连线"]');
  const secondEndInput = page.locator('.react-flow__node[data-id="@@canvas-end-2"] .react-flow__handle[data-handleid="input"]');
  await doneOutput.dragTo(node2Input, { steps: 12 });
  await node2Output.dragTo(firstEndInput, { steps: 12 });
  await reviewOutput.dragTo(secondEndInput, { steps: 12 });
  await expect(page.locator(".react-flow__edge")).toHaveCount(3);
  const doneEdge = page.locator('.react-flow__edge[data-id="transition-0"]');
  await expect(doneEdge).toHaveAttribute("aria-label", "Edge from @@agent-node-0 to @@agent-node-1");
  // 测试布局里多条连线的端点重连热区与标签重叠，直接向连线派发点击，只验证选中后的属性面板。
  await doneEdge.dispatchEvent("click");
  await expect(page.getByRole("region", { name: "转移属性" })).toContainText("done");
  await page.keyboard.press("Delete");
  await expect(page.locator(".react-flow__edge")).toHaveCount(2);
  await page.keyboard.press("Control+z");
  await expect(page.locator(".react-flow__edge")).toHaveCount(3);
  const needsReviewEdge = page.locator('.react-flow__edge[data-id="transition-2"]');
  await needsReviewEdge.dispatchEvent("click");
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
  const node2 = page.locator('.react-flow__node[data-id="@@agent-node-1"]');
  const nodePositionBeforeDrag = await node2.evaluate(element => element.style.transform);
  const node2Bounds = await node2.boundingBox();
  expect(node2Bounds).not.toBeNull();
  await page.mouse.move(node2Bounds!.x + node2Bounds!.width / 2, node2Bounds!.y + node2Bounds!.height / 2);
  await page.mouse.down();
  await page.mouse.move(node2Bounds!.x + node2Bounds!.width / 2 + 160, node2Bounds!.y + node2Bounds!.height / 2 + 110, { steps: 12 });
  await page.mouse.up();
  await expect(page.locator('.react-flow__node[data-id="@@agent-node-1"]')).toBeVisible();
  await expect(page.locator(".react-flow__node")).toHaveCount(4);
  for (const node of await page.locator(".react-flow__node").all()) await expect(node).toBeVisible();
  await expect.poll(() => node2.evaluate(element => element.style.transform)).not.toBe(nodePositionBeforeDrag);
  const nodePositionAfterDrag = await node2.evaluate(element => element.style.transform);
  await page.getByRole("button", { name: "撤销", exact: true }).click();
  await expect.poll(() => node2.evaluate(element => element.style.transform)).toBe(nodePositionBeforeDrag);
  await page.getByRole("button", { name: "重做", exact: true }).click();
  await expect.poll(() => node2.evaluate(element => element.style.transform)).toBe(nodePositionAfterDrag);
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
  // 大纲视图里入口节点卡片带 START 标签，取代原先的「入口节点」下拉框。
  await expect(page.getByRole("article", { name: "节点 代码审查" })).toContainText("START");
  await page.getByRole("button", { name: "画布" }).click();
  await page.getByRole("button", { name: "Team 属性", exact: true }).click();
  await expect(page.getByRole("region", { name: "Team 属性" }).getByLabel("Team ID")).toHaveValue("review_flow");
  await expect(page.locator('.react-flow__node[data-id^="@@canvas-end-"]')).toHaveCount(2);
  await expect(page.locator(".react-flow__edge")).toHaveCount(3);
  await expect(page.getByRole("button", { name: "撤销" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "重做" })).toBeEnabled();
  await page.getByRole("button", { name: "重做" }).click();
  await expect(page.locator('.react-flow__node[data-id="@@agent-node-1"]')).toHaveCount(0);
  await page.getByRole("button", { name: "撤销" }).click();
  await expect(page.locator('.react-flow__node[data-id="@@agent-node-1"]')).toBeVisible();
  expect(await page.evaluate(() => Object.keys(localStorage).some(key => key.includes("agent-team-layout")))).toBe(false);
  // 从表单再次切回画布，同样默认全屏。
  await expect(page.getByRole("region", { name: "Agent Team 画布编辑器" })).toHaveClass(/fixed/);
  const flowElement = await page.locator(".react-flow").elementHandle();
  await page.getByRole("button", { name: "保存", exact: true }).click();

  await expect.poll(() => savedConfiguration).not.toBeNull();
  const persistedConfiguration = savedConfiguration as unknown as Record<string, unknown>;
  expect(persistedConfiguration).toMatchObject({
    team_id: "review_flow",
    start_node_id: "node_1",
    nodes: [
      // 画布新建节点与新增状态都使用 DEFAULT_AGENT_TEAM_STATUS_COLOR，未手动改色。
      { node_id: "node_1", name: "代码审查", agent_id: "reviewer", statuses: [{ name: "done", color: "#3B82F6" }, { name: "needs_review", color: "#3B82F6" }] },
      { node_id: "node_2", name: "reviewer", agent_id: "reviewer", statuses: [{ name: "done", color: "#3B82F6" }] },
    ],
  });
  expect(persistedConfiguration.transitions).toEqual(expect.arrayContaining([
    { from_node_id: "node_1", status: "done", target_node_id: "node_2" },
    { from_node_id: "node_2", status: "done", target_node_id: "END" },
    { from_node_id: "node_1", status: "needs_review", target_node_id: "node_2" },
  ]));
  await expect.poll(() => page.evaluate(() => Object.keys(window.localStorage).some((key) => key.includes("agent-team-layout-v2") && key.endsWith(":review_flow")))).toBe(true);
  expect(await page.evaluate(() => Object.keys(window.localStorage).some((key) => key.includes("agent-team-layout-v2") && key.endsWith(":new")))).toBe(false);
  // 保存成功是一次性结果，走 toast，不再弹出需要关闭的对话框。
  await expect(page.getByText("Agent Team 配置已保存", { exact: true })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Agent Team 画布编辑器" })).toHaveClass(/fixed/);
  expect(await flowElement!.evaluate(element => element.isConnected)).toBe(true);
  await expect(page.getByRole("button", { name: "撤销", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "返回 Team 编辑" }).click();
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
  await page.getByRole("navigation", { name: "系统配置分类" }).getByRole("link", { name: "Agent Team", exact: true }).click();
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

test("在节点卡片里添加转移不弹前端校验提示，未完成草稿提交给后端", async ({ page }) => {
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
  await page.getByRole("navigation", { name: "系统配置分类" }).getByRole("link", { name: "Agent Team", exact: true }).click();
  await page.getByRole("button", { name: "新建 Team" }).click();
  await page.getByRole("button", { name: "添加节点" }).click();
  await expect(page.locator("[data-team-entry-card]")).toHaveCount(1);
  await page.getByRole("button", { name: "为 节点 1 添加转移" }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.getByLabel("Team ID").fill("draft_team");
  await page.getByLabel("Team 名称").fill("Draft Team");
  await page.getByLabel("用途说明").fill("提交未完成草稿");
  await page.getByRole("button", { name: "保存配置" }).click();

  const saveFailure = page.getByRole("dialog");
  await expect(saveFailure.getByText("后端校验：请至少添加一个 Team 节点")).toBeVisible();
  expect((submittedConfiguration as unknown as { transitions: unknown[] }).transitions)
    .toEqual([{ from_node_id: "node_1", status: "done", target_node_id: "END" }]);
});

test("连续点 + 添加的节点都落在属性面板左侧的可见区域", async ({ page }) => {
  await page.route("**/configuration/agents", (route) => route.fulfill({ json: [{
    agent_id: "reviewer", role: "child", description: "", system_prompt: "", allowed_tool_groups: [], max_steps: 20,
    model_config_id: null, model_settings: {}, source: "user_file", path: ".cosir/agents/reviewer.json", editable: true,
    deletable: true, validation_status: "valid", validation_error: null, file_name: "reviewer.json",
  }] }));
  await page.route("**/configuration/agent-teams", (route) => route.fulfill({ json: [] }));
  await page.goto("/settings");
  await page.getByRole("navigation", { name: "系统配置分类" }).getByRole("link", { name: "Agent Team", exact: true }).click();
  await page.getByRole("button", { name: "新建 Team" }).click();
  await page.getByRole("button", { name: "画布" }).click();
  await expect(page.getByRole("region", { name: "Agent Team 画布编辑器" })).toHaveClass(/fixed/);
  // 避让搜索每圈先向右挪；画布中心被占后，新节点会被推进右侧属性面板的覆盖范围。
  for (let index = 0; index < 4; index += 1) {
    await page.getByRole("button", { name: "添加 reviewer" }).click();
    const inspector = await page.getByRole("complementary", { name: "属性面板" }).boundingBox();
    const canvas = await page.locator("[data-agent-drop-zone]").boundingBox();
    await expect.poll(async () => {
      const box = await page.locator(`.react-flow__node[data-id="@@agent-node-${index}"]`).boundingBox();
      return box!.x >= canvas!.x && box!.x + box!.width <= inspector!.x && box!.y >= canvas!.y && box!.y + box!.height <= canvas!.y + canvas!.height;
    }, { message: `第 ${index + 1} 个节点应完整落在可见区域` }).toBe(true);
  }
});
