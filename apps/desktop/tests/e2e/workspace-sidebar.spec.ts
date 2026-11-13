import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";

import { seedState } from "./transport-fixtures";
import { API_ORIGIN as origin, WEB_ORIGIN } from "./e2e-env";
import { installFakeDesktopHost } from "./fake-desktop-host";

const longTitle = "检查工作区任务列表中的超长中文标题与EnglishIdentifierWithoutSpaces".repeat(3);
const workspaceName = "cosir-用于验证长名称的本地工作区";

/** 状态推送的整表帧：每个 Task 的执行状态与等你操作的调用数（缺省 0）。 */
function statusFrame(statuses: Record<number, string | null>, awaiting: Record<number, number> = {}) {
  const tasks = Object.fromEntries(Object.entries(statuses).map(([id, executionStatus]) => (
    [id, { executionStatus, awaitingUserCalls: awaiting[Number(id)] ?? 0 }]
  )));
  return `${JSON.stringify({ type: "full", tasks })}\n`;
}

async function seedSidebar(
  page: Page,
  request: APIRequestContext,
  theme: string,
  statuses: Record<number, string | null> = { 100: null, 101: "suspended", 102: "running" },
  awaiting: Record<number, number> = {},
) {
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
    { task_id: 102, workspace_id: 7, title: "分支任务", task_type: "user_fork", extra: { fork: { tree_parent_task_id: 100 } }, created_at: updatedAt, updated_at: updatedAt },
  ];
  await page.route(`${origin}/workspaces/7/tasks`, (route) => route.fulfill({ json: tasks }));
  // 状态点只读状态推送：一帧整表即可（流结束后客户端重连，再拿到同一张表）。
  await page.route(`${origin}/task-feed/stream`, (route) => route.fulfill({
    contentType: "application/x-ndjson", body: statusFrame(statuses, awaiting),
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
    await expect(other.getByRole("img", { name: "等待结果" })).toBeVisible();
    await expect(row(page, "分支任务").getByRole("img", { name: "运行中" })).toBeVisible();

    const title = selected.locator(".workspace-tree-title");
    // 当前任务只靠底色标出，不加粗；分组名与任务标题左对齐（图标格与状态标记列同宽）。
    await expect(title).toHaveCSS("font-weight", "400");
    expect((await box(workspace.locator(".workspace-tree-title"))).x).toBeCloseTo((await box(title)).x, 0);
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
    // 悬停卡片给出完整标题；工作区的卡片带出界面上别处看不到的完整路径。
    await expect(page.locator('[data-slot="tooltip-content"][data-open]')).toContainText(longTitle);
    await workspace.hover();
    await expect(page.locator('[data-slot="tooltip-content"][data-open]')).toContainText("/workspace/cosir");
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

test("点工作区行只展开收起、不改选中工作区；「+」在该工作区开始新对话", async ({ page, request }) => {
  await seedSidebar(page, request, "light");
  await page.goto("/tasks/100");
  const other = row(page, "其他工作区");
  await other.click();
  await expect(page).toHaveURL(/\/tasks\/100$/);
  // 行点击不再悄悄改写选中工作区：回到新对话页仍是上次的工作区。
  await page.getByRole("button", { name: "新对话", exact: true }).click();
  await expect(page.getByRole("button", { name: `选择工作区，当前 ${workspaceName}` })).toBeVisible();

  await page.goto("/tasks/100");
  await other.hover();
  await other.getByRole("button", { name: "在 其他工作区 中新建对话" }).click();
  await expect(page).toHaveURL(`${WEB_ORIGIN}/`);
  await expect(page.getByRole("button", { name: "选择工作区，当前 其他工作区" })).toBeVisible();
});

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

/** 工作区 7 下 `count` 个顶层任务，id 越大越新（侧栏按最近更新倒序，先显示 200+count-1）。 */
async function seedManyTasks(
  page: Page,
  request: APIRequestContext,
  count: number,
  statuses: Record<number, string | null> = {},
  awaiting: Record<number, number> = {},
) {
  await seedSidebar(page, request, "light");
  const many = Array.from({ length: count }, (_, index) => {
    const at = new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString();
    return { task_id: 200 + index, workspace_id: 7, title: `批量任务 ${index}`, task_type: "user", created_at: at, updated_at: at };
  });
  await page.route(`${origin}/workspaces/7/tasks`, (route) => route.fulfill({ json: many }));
  await page.route(`${origin}/task-feed/stream`, (route) => route.fulfill({
    contentType: "application/x-ndjson", body: statusFrame(statuses, awaiting),
  }));
  for (const task of many) {
    await page.route(`${origin}/tasks/${task.task_id}`, (route) => route.fulfill({ json: task }));
    await seedState(request, task.task_id, { title: task.title, messages: [], activity: null });
  }
}

test("每个工作区默认只显示最近 5 个任务，可展开其余并收起", async ({ page, request }) => {
  await seedManyTasks(page, request, 8);
  await page.goto("/");
  await expect(row(page, "批量任务 7")).toBeVisible();
  await expect(row(page, "批量任务 3")).toBeVisible();
  await expect(row(page, "批量任务 2")).toHaveCount(0);

  await row(page, "展开其余 3 个").click();
  await expect(row(page, "批量任务 0")).toBeVisible();
  await row(page, "收起").click();
  await expect(row(page, "批量任务 2")).toHaveCount(0);
  await expect(row(page, "展开其余 3 个")).toBeVisible();
});

test("运行中与打开过的任务留在可见范围内，离开后也不消失", async ({ page, request }) => {
  await seedManyTasks(page, request, 12, { 201: "running" });
  await page.goto("/tasks/202");
  // 202 排在第 10 位：打开它把水位线推到第 10 个；201 正在运行，推到第 11 个。
  await expect(row(page, "批量任务 2")).toHaveAttribute("aria-current", "page");
  await expect(row(page, "批量任务 1")).toBeVisible();
  await expect(row(page, "批量任务 0")).toHaveCount(0);
  await expect(row(page, "展开其余 1 个")).toBeVisible();

  await page.getByRole("button", { name: "新对话", exact: true }).click();
  await expect(page).toHaveURL(`${WEB_ORIGIN}/`);
  await expect(row(page, "批量任务 2")).toBeVisible();
});

test("有事等你操作时状态位与行尾都写明，Run 已完成也不例外；页头两项并列", async ({ page, request }) => {
  await seedSidebar(page, request, "light", { 100: null, 101: "completed", 102: "running" }, { 101: 2 });
  await page.goto("/tasks/101");
  const waiting = row(page, "核对运行状态");
  await expect(waiting.getByRole("img", { name: "等你操作 · 2" })).toBeVisible();
  await expect(waiting).toContainText("等你操作");
  // 没有待决事项的行照旧：运行中是圆点，行尾是相对时间。
  await expect(row(page, "分支任务").getByRole("img", { name: "运行中" })).toBeVisible();
  const header = page.locator("header");
  await expect(header.getByText("等你操作 · 2")).toBeVisible();
  await expect(header.getByText("已完成")).toHaveCount(0);
});

test("有事等你操作的任务留在可见范围内，即使它的 Run 早已结束", async ({ page, request }) => {
  await seedManyTasks(page, request, 12, { 201: "completed" }, { 201: 1 });
  await page.goto("/");
  await expect(row(page, "批量任务 1")).toBeVisible();
  await expect(row(page, "批量任务 0")).toHaveCount(0);
});

test("区头：搜索打开命令面板，添加工作区后在新工作区开始新对话", async ({ page, request }) => {
  await installFakeDesktopHost(page, { directory: "/Users/me/projects/新项目" });
  await seedSidebar(page, request, "light");
  const at = new Date().toISOString();
  const existing = [
    { workspace_id: 7, name: workspaceName, root_path: "/workspace/cosir", created_at: at, updated_at: at },
    { workspace_id: 8, name: "其他工作区", root_path: "/workspace/other", created_at: at, updated_at: at },
  ];
  const created: { name: string; root_path: string }[] = [];
  await page.route(`${origin}/workspaces`, async (route) => {
    if (route.request().method() === "POST") {
      created.push(route.request().postDataJSON());
      await route.fulfill({ json: { workspace_id: 9, ...created[0], created_at: at, updated_at: at } });
      return;
    }
    await route.fulfill({ json: created.length === 0 ? existing : [...existing, { workspace_id: 9, ...created[0], created_at: at, updated_at: at }] });
  });
  await page.route(`${origin}/workspaces/9/tasks`, (route) => route.fulfill({ json: [] }));
  await page.goto("/tasks/100");

  await page.getByRole("button", { name: "搜索任务", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "命令面板" })).toBeVisible();
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "添加工作区", exact: true }).click();
  await expect(row(page, "新项目")).toBeVisible();
  expect(created).toEqual([{ name: "新项目", root_path: "/Users/me/projects/新项目" }]);
  await expect(page).toHaveURL(`${WEB_ORIGIN}/`);
  await expect(page.getByRole("button", { name: "选择工作区，当前 新项目" })).toBeVisible();
  await expect(row(page, "新项目")).toHaveAttribute("aria-current", "true");
});
