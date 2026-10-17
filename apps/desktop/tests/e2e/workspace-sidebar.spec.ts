import { expect, test, type Locator, type Page } from "@playwright/test";

const origin = "http://127.0.0.1:8000";
const longTitle = "检查工作区任务列表中的超长中文标题与EnglishIdentifierWithoutSpaces".repeat(3);
const workspaceName = "cosir-用于验证长名称的本地工作区";

async function seedSidebar(page: Page, theme: string) {
  await page.addInitScript((value) => {
    localStorage.clear();
    localStorage.setItem("cosir.theme-preference", value);
    localStorage.setItem("cosir:last-workspace-id", "7");
  }, theme);
  const updatedAt = new Date(Date.now() - 44 * 60_000).toISOString();
  const workspace = { workspace_id: 7, name: workspaceName, root_path: "/workspace/cosir", created_at: updatedAt, updated_at: updatedAt };
  await page.route(`${origin}/workspaces`, (route) => route.fulfill({ json: [workspace, { ...workspace, workspace_id: 8, name: "其他工作区", root_path: "/workspace/other" }] }));
  await page.route(`${origin}/workspaces/8/tasks`, (route) => route.fulfill({ json: [] }));
  const tasks = [
    { task_id: 100, workspace_id: 7, title: longTitle, task_type: "user", execution_status: null, created_at: updatedAt, updated_at: updatedAt },
    { task_id: 101, workspace_id: 7, title: "核对运行状态", task_type: "user", execution_status: "waiting_for_input", created_at: updatedAt, updated_at: "2026-01-01T00:00:00Z" },
    { task_id: 102, workspace_id: 7, title: "分支任务", task_type: "fork", extra: { fork: { source_task_id: 100 } }, execution_status: "running", created_at: updatedAt, updated_at: updatedAt },
  ];
  await page.route(`${origin}/workspaces/7/tasks`, (route) => route.fulfill({ json: tasks }));
  // 只读展示夹具按浏览器隔离，避免其他测试重置共享内存服务时影响任务切换。
  for (const task of tasks) {
    await page.route(`${origin}/tasks/${task.task_id}`, (route) => route.fulfill({ json: task }));
    await page.route(`${origin}/tasks/${task.task_id}/assistant/state`, (route) => route.fulfill({ json: {
      runs: [], current_run_id: null, approvals: {}, context_window_total: null, error: null,
    } }));
  }
}

function row(page: Page, name: string) {
  return page.getByRole("treeitem", { name, exact: true });
}

async function box(locator: Locator) {
  const result = await locator.boundingBox();
  expect(result).not.toBeNull();
  return result!;
}

for (const theme of ["light", "dark"]) {
  test(`${theme}：长标题不溢出，选中与悬停分离，菜单不引起位移`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await seedSidebar(page, theme);
    await page.goto("/tasks/100");
    const selected = row(page, longTitle);
    const other = row(page, "核对运行状态");
    const workspace = row(page, workspaceName);
    await expect(selected).toHaveAttribute("aria-current", "page");
    await expect(workspace).toHaveAttribute("aria-selected", "false");
    await expect(page.locator("html")).toHaveCSS("color-scheme", theme);
    await expect(other.getByRole("img", { name: "等待确认" })).toBeVisible();
    await expect(row(page, "分支任务").getByRole("img", { name: "运行中" })).toBeVisible();

    const title = selected.locator(".workspace-tree-title");
    await expect(title).toHaveAttribute("title", longTitle);
    const dimensions = await title.evaluate((element) => ({
      client: element.clientWidth, scroll: element.scrollWidth,
      whiteSpace: getComputedStyle(element).whiteSpace,
      overflow: getComputedStyle(element).textOverflow,
    }));
    expect(dimensions.scroll).toBeGreaterThan(dimensions.client);
    expect(dimensions.whiteSpace).toBe("nowrap");
    expect(dimensions.overflow).toBe("ellipsis");
    const titleBefore = await box(title);
    expect(titleBefore.height).toBeLessThan((await box(selected)).height);
    const aside = await box(page.locator("aside").first());
    for (const item of [workspace, selected, other]) {
      const bounds = await box(item);
      expect(bounds.x + bounds.width).toBeLessThanOrEqual(aside.x + aside.width);
    }
    const selectedColor = await selected.evaluate((element) => getComputedStyle(element).backgroundColor);
    await other.hover();
    await expect.poll(() => other.evaluate((element) => getComputedStyle(element).backgroundColor)).not.toBe(selectedColor);
    await expect(other).toHaveAttribute("aria-selected", "false");

    await selected.hover();
    const more = selected.getByRole("button", { name: `更多 ${longTitle} 操作`, exact: true });
    await more.click();
    await page.mouse.move(900, 500);
    await expect(more).toHaveAttribute("aria-expanded", "true");
    await expect(selected.locator(".workspace-tree-actions")).toHaveCSS("opacity", "1");
    await expect(selected.locator("time")).toHaveCSS("visibility", "hidden");
    expect(await box(title)).toEqual(titleBefore);
    await page.keyboard.press("Escape");
    await expect(more).toBeFocused();
    await page.getByRole("navigation", { name: "当前位置" }).click();
    await page.locator("aside").first().screenshot({ path: testInfo.outputPath(`${theme}-sidebar.png`) });

    await page.getByRole("button", { name: "新对话", exact: true }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(workspace).toHaveAttribute("aria-current", "true");
    await expect(selected).toHaveAttribute("aria-selected", "false");
    await expect(page.getByRole("treeitem", { selected: true })).toHaveCount(1);
  });

  test(`${theme}：方向键、Enter、Tab、空格和 Escape 各自执行正确操作`, async ({ page }) => {
    await seedSidebar(page, theme);
    await page.goto("/");
    const workspace = row(page, workspaceName);
    const selected = row(page, longTitle);
    await expect(workspace).toHaveAttribute("aria-current", "true");
    await page.getByRole("tree", { name: "工作区和任务列表" }).focus();
    await expect(workspace).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await expect(selected).toHaveCount(0);
    await page.keyboard.press("ArrowRight");
    await expect(selected).toBeVisible();
    await page.keyboard.press("ArrowDown");
    await expect(selected).toBeFocused();
    await expect(workspace).toHaveAttribute("aria-current", "true");
    await expect(selected).toHaveCSS("outline-style", "solid");
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/tasks\/100$/);
    await expect(selected).toHaveAttribute("aria-current", "page");
    await expect(page.getByLabel("消息输入")).toBeVisible();
    await expect(selected).toBeFocused();

    await page.keyboard.press("Tab");
    const more = selected.getByRole("button", { name: `更多 ${longTitle} 操作`, exact: true });
    await expect(more).toBeFocused();
    await page.keyboard.press("Space");
    await expect(more).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByRole("button", { name: "删除", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(more).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "系统配置", exact: true })).toBeFocused();
    await expect(page).toHaveURL(/\/tasks\/100$/);

    await page.getByRole("tree").focus();
    await page.keyboard.press("ArrowDown");
    await expect(row(page, "分支任务")).toBeFocused();
    await expect(selected).toHaveAttribute("aria-current", "page");
    await page.keyboard.press("Space");
    await expect(page).toHaveURL(/\/tasks\/102$/);
  });
}
