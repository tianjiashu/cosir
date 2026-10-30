import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";

import { seedState } from "./transport-fixtures";

const origin = "http://127.0.0.1:8000";
const longTitle = "检查工作区任务列表中的超长中文标题与EnglishIdentifierWithoutSpaces".repeat(3);
const workspaceName = "cosir-用于验证长名称的本地工作区";

async function seedSidebar(page: Page, request: APIRequestContext, theme: string) {
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
    { task_id: 100, workspace_id: 7, title: longTitle, task_type: "user", created_at: updatedAt, updated_at: updatedAt },
    { task_id: 101, workspace_id: 7, title: "核对运行状态", task_type: "user", created_at: updatedAt, updated_at: "2026-01-01T00:00:00Z" },
    { task_id: 102, workspace_id: 7, title: "分支任务", task_type: "fork", extra: { fork: { source_task_id: 100 } }, created_at: updatedAt, updated_at: updatedAt },
  ];
  await page.route(`${origin}/workspaces/7/tasks`, (route) => route.fulfill({ json: tasks }));
  // 状态点只读状态推送：一帧整表即可（流结束后客户端重连，再拿到同一张表）。
  const statuses = { 100: null, 101: "suspended", 102: "running" };
  await page.route(`${origin}/task-status/stream`, (route) => route.fulfill({
    contentType: "application/x-ndjson", body: `${JSON.stringify({ type: "full", statuses })}\n`,
  }));
  // 只读展示夹具按浏览器隔离，避免其他测试重置共享内存服务时影响任务切换。
  for (const task of tasks) {
    await page.route(`${origin}/tasks/${task.task_id}`, (route) => route.fulfill({ json: task }));
    // 对话 state 与 Task 长订阅由模拟后端持有：订阅需要一条常驻连接，路由夹具无法保持。
    await seedState(request, task.task_id, { title: "测试对话", messages: [], activity: null });
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
  test(`${theme}：长标题不溢出，选中与悬停分离，行内删除操作不引起位移`, async ({ page, request }, testInfo) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await seedSidebar(page, request, theme);
    await page.goto("/tasks/100");
    const selected = row(page, longTitle);
    const other = row(page, "核对运行状态");
    const workspace = row(page, workspaceName);
    await expect(selected).toHaveAttribute("aria-current", "page");
    await expect(workspace).toHaveAttribute("aria-selected", "false");
    await expect(page.locator("html")).toHaveCSS("color-scheme", theme);
    await expect(other.getByRole("img", { name: "等待中" })).toBeVisible();
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
    const menuButton = selected.getByRole("button", { name: `更多操作：${longTitle}`, exact: true });
    await expect(menuButton).toBeVisible();
    await expect(selected.locator(".workspace-tree-actions")).toHaveCSS("opacity", "1");
    await expect(selected.locator("time")).toHaveCSS("visibility", "hidden");
    expect(await box(title)).toEqual(titleBefore);
    await page.getByRole("navigation", { name: "当前位置" }).click();
    await page.locator("aside").first().screenshot({ path: testInfo.outputPath(`${theme}-sidebar.png`) });

    await page.getByRole("button", { name: "新对话", exact: true }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(workspace).toHaveAttribute("aria-current", "true");
    await expect(selected).toHaveAttribute("aria-selected", "false");
    await expect(page.getByRole("treeitem", { selected: true })).toHaveCount(1);
  });

  test(`${theme}：方向键、Enter、Tab、空格和 Escape 各自执行正确操作`, async ({ page, request }) => {
    await seedSidebar(page, request, theme);
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
    const menuButton = selected.getByRole("button", { name: `更多操作：${longTitle}`, exact: true });
    await expect(menuButton).toBeFocused();
    await page.keyboard.press("Space");
    await expect(page.getByRole("menuitem", { name: "置顶" })).toBeFocused();
    await page.keyboard.press("ArrowDown");
    // radix 在下一个任务里才移动菜单焦点，确认落到「删除」再按 Enter。
    await expect(page.getByRole("menuitem", { name: "删除" })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { name: `删除任务「${longTitle}」？` })).toBeVisible();
    // 它有一个 fork：询问只删这个还是一起删。
    await expect(page.getByRole("button", { name: "只删除这个", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "一起删除 2 个任务", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(menuButton).toBeFocused();
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

test("Fork 挂在源任务下：缩进一层，可点击或用方向键折叠", async ({ page, request }) => {
  await seedSidebar(page, request, "light");
  await page.goto("/");
  const source = row(page, longTitle);
  const forked = row(page, "分支任务");
  await expect(source).toHaveAttribute("aria-level", "2");
  await expect(forked).toHaveAttribute("aria-level", "3");
  // 嵌套的 Fork 由层级表达身份，不再重复显示 Fork 图标。
  await expect(forked.getByRole("img", { name: "Fork 任务" })).toHaveCount(0);
  expect((await box(forked.locator(".workspace-tree-title"))).x).toBeGreaterThan((await box(source.locator(".workspace-tree-title"))).x);

  const toggle = source.getByRole("button", { name: `收起 ${longTitle} 的 Fork`, exact: true });
  await toggle.click();
  await expect(forked).toHaveCount(0);
  await expect(page).toHaveURL(/\/$/);
  await expect(source.getByRole("button", { name: `展开 ${longTitle} 的 Fork`, exact: true })).toHaveAttribute("aria-expanded", "false");


  // 点标题打开源任务并让它成为树的焦点行，再用方向键展开 / 收起。
  await source.locator(".workspace-tree-title").click();
  await expect(page).toHaveURL(/\/tasks\/100$/);
  await expect(source).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(forked).toBeVisible();
  await page.keyboard.press("ArrowLeft");
  await expect(forked).toHaveCount(0);
});

test("置顶：顶层任务连同 Fork 移到置顶区并写入偏好，取消后回到工作区", async ({ page, request }) => {
  await seedSidebar(page, request, "light");
  await page.goto("/");
  const source = row(page, longTitle);
  const pinnedGroup = row(page, "置顶");
  await expect(pinnedGroup).toHaveCount(0);

  await source.hover();
  await source.getByRole("button", { name: `更多操作：${longTitle}`, exact: true }).click();
  await page.getByRole("menuitem", { name: "置顶" }).click();
  await expect(pinnedGroup).toHaveAttribute("aria-level", "1");
  await expect(source).toHaveAttribute("aria-level", "2");
  // 置顶区排在工作区之前，Fork 跟随源任务移动。
  const order = await page.getByRole("treeitem").evaluateAll((items) => items.map((item) => item.getAttribute("aria-label")));
  expect(order.slice(0, 4)).toEqual(["置顶", longTitle, "分支任务", workspaceName]);

  // 只有顶层任务可置顶，Fork 的菜单里没有置顶项。
  const forked = row(page, "分支任务");
  await forked.hover();
  await forked.getByRole("button", { name: "更多操作：分支任务", exact: true }).click();
  await expect(page.getByRole("menuitem", { name: "删除" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "置顶" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  // 夹具每次导航都会清空 localStorage，无法跨刷新验证，这里直接断言偏好已写入。
  expect(await page.evaluate(() => localStorage.getItem("cosir:pinned-tasks:v1"))).toBe("[100]");

  await source.hover();
  await source.getByRole("button", { name: `更多操作：${longTitle}`, exact: true }).click();
  await page.getByRole("menuitem", { name: "取消置顶" }).click();
  await expect(pinnedGroup).toHaveCount(0);
  await expect(source).toHaveAttribute("aria-level", "2");
  expect(await page.evaluate(() => localStorage.getItem("cosir:pinned-tasks:v1"))).toBe("[]");
});

test("拖拽侧栏右缘调宽：最宽时 20 字标题完整显示，宽度写入偏好", async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await seedSidebar(page, request, "light");
  const twentyChars = "检查侧栏标题在最大宽度下能否完整显示二十";
  const now = new Date().toISOString();
  const titled = { task_id: 103, workspace_id: 7, title: twentyChars, task_type: "user", created_at: now, updated_at: now };
  // 后注册的路由优先：只给这条用例追加一个 20 字标题的任务。
  await page.route(`${origin}/workspaces/7/tasks`, (route) => route.fulfill({ json: [titled] }));
  await page.route(`${origin}/tasks/103`, (route) => route.fulfill({ json: titled }));
  await seedState(request, 103, { title: twentyChars, messages: [], activity: null });
  await page.goto("/tasks/103");

  const aside = page.locator("aside").first();
  const handle = page.getByRole("separator", { name: "调整侧栏宽度" });
  const title = row(page, twentyChars).locator(".workspace-tree-title");
  const overflows = () => title.evaluate((element) => element.scrollWidth > element.clientWidth);
  await expect(title).toBeVisible();
  expect((await box(aside)).width).toBeCloseTo(288, 0);
  expect(await overflows()).toBe(true);

  const grip = await box(handle);
  await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
  await page.mouse.down();
  await page.mouse.move(grip.x + grip.width / 2 + 300, grip.y + grip.height / 2, { steps: 5 });
  await page.mouse.up();
  await expect(handle).toHaveAttribute("aria-valuenow", "420");
  await expect.poll(async () => (await box(aside)).width).toBeCloseTo(420, 0);
  expect(await overflows()).toBe(false);

  await handle.focus();
  await page.keyboard.press("Home");
  await expect(handle).toHaveAttribute("aria-valuenow", "240");
  // 夹具每次导航都会清空 localStorage，这里直接断言写入的偏好，而不是刷新后读回。
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("cosir:layout") ?? "null")?.state?.sidebarWidth)).toBe(240);
});

test("尾栏按时间实际宽度让位：默认宽度下短时间的行能放下更长标题，悬停换成操作也不位移", async ({ page, request }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await seedSidebar(page, request, "light");
  const fitting = "调研沙箱改Python可行性";
  const now = new Date(Date.now() - 60_000).toISOString();
  const titled = { task_id: 104, workspace_id: 7, title: fitting, task_type: "user", created_at: now, updated_at: now };
  await page.route(`${origin}/workspaces/7/tasks`, (route) => route.fulfill({ json: [titled] }));
  await page.route(`${origin}/tasks/104`, (route) => route.fulfill({ json: titled }));
  await seedState(request, 104, { title: fitting, messages: [], activity: null });
  await page.goto("/tasks/104");

  const target = row(page, fitting);
  const title = target.locator(".workspace-tree-title");
  await expect(title).toBeVisible();
  expect(await title.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  const before = await box(title);
  await target.hover();
  await expect(target.getByRole("button", { name: `更多操作：${fitting}`, exact: true })).toBeVisible();
  expect(await box(title)).toEqual(before);
});
