import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";

import { seedState } from "./transport-fixtures";
import { API_ORIGIN as origin, WEB_ORIGIN } from "./e2e-env";
import { installFakeDesktopHost } from "./fake-desktop-host";

const longTitle = "检查工作区会话列表中的超长中文标题与EnglishIdentifierWithoutSpaces".repeat(3);
const workspaceName = "cosir-用于验证长名称的本地工作区";

type StatusExtra = Partial<{ awaitingDecisions: number; awaitingApprovals: number; unread: boolean }>;

/** 状态推送的整表帧：每个 Session 的执行状态，另可给在等你的调用数与未读（缺省 0 与已读）。 */
function statusFrame(statuses: Record<number, string | null>, extra: Record<number, StatusExtra> = {}) {
  const sessions = Object.fromEntries(Object.entries(statuses).map(([id, executionStatus]) => (
    [id, { executionStatus, awaitingDecisions: 0, awaitingApprovals: 0, unread: false, ...extra[Number(id)] }]
  )));
  return `${JSON.stringify({ type: "full", sessions })}\n`;
}

async function seedSidebar(
  page: Page,
  request: APIRequestContext,
  theme: string,
  statuses: Record<number, string | null> = { 100: null, 101: "suspended", 102: "running" },
  extra: Record<number, StatusExtra> = {},
) {
  await page.addInitScript((value) => {
    localStorage.clear();
    localStorage.setItem("cosir:theme", JSON.stringify({ state: { preference: value }, version: 0 }));
    localStorage.setItem("cosir:last-workspace", JSON.stringify({ state: { workspaceId: 7 }, version: 0 }));
  }, theme);
  const updatedAt = new Date(Date.now() - 44 * 60_000).toISOString();
  const workspace = { workspace_id: 7, name: workspaceName, root_path: "/workspace/cosir", created_at: updatedAt, updated_at: updatedAt };
  await page.route(`${origin}/workspaces`, (route) => route.fulfill({ json: [workspace, { ...workspace, workspace_id: 8, name: "其他工作区", root_path: "/workspace/other" }] }));
  await page.route(`${origin}/workspaces/8/sessions`, (route) => route.fulfill({ json: [] }));
  const sessions = [
    { session_id: 100, workspace_id: 7, title: longTitle, session_type: "user", created_at: updatedAt, updated_at: updatedAt },
    { session_id: 101, workspace_id: 7, title: "核对运行状态", session_type: "user", created_at: updatedAt, updated_at: "2026-01-01T00:00:00Z" },
    { session_id: 102, workspace_id: 7, title: "分支会话", session_type: "user_fork", extra: { fork: { tree_parent_session_id: 100 } }, created_at: updatedAt, updated_at: updatedAt },
  ];
  await page.route(`${origin}/workspaces/7/sessions`, (route) => route.fulfill({ json: sessions }));
  // 状态点只读状态推送：一帧整表即可（流结束后客户端重连，再拿到同一张表）。
  await page.route(`${origin}/session-feed/stream`, (route) => route.fulfill({
    contentType: "application/x-ndjson", body: statusFrame(statuses, extra),
  }));
  // 只读展示夹具按浏览器隔离，避免其他测试重置共享内存服务时影响会话切换。
  for (const session of sessions) {
    await page.route(`${origin}/sessions/${session.session_id}`, (route) => route.fulfill({ json: session }));
    // 对话 state 与 Session 长订阅由模拟后端持有：订阅需要一条常驻连接，路由夹具无法保持。
    await seedState(request, session.session_id, { title: "测试对话", messages: [], activity: null });
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
    // 关掉底色过渡（侧栏样式按 reduced-motion 关闭），读到的计算色就是终值。
    await page.emulateMedia({ reducedMotion: "reduce" });
    await seedSidebar(page, request, theme);
    await page.goto("/sessions/100");
    const selected = row(page, longTitle);
    const other = row(page, "核对运行状态");
    const workspace = row(page, workspaceName);
    await expect(selected).toHaveAttribute("aria-current", "page");
    await expect(workspace).toHaveAttribute("aria-selected", "false");
    await expect(page.locator("html")).toHaveCSS("color-scheme", theme);
    await expect(other.getByRole("img", { name: "进行中，在等后台结果" })).toBeVisible();
    await expect(row(page, "分支会话").getByRole("img", { name: "进行中" })).toBeVisible();

    const title = selected.locator(".workspace-tree-title");
    // 当前会话只靠底色标出，不加粗；分组名与会话标题左对齐（图标格与状态标记列同宽）。
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
    const hoverColor = await other.evaluate((element) => getComputedStyle(element).backgroundColor);

    // 入口行与会话树同源：悬停底色相同，图标与工作区图标在同一条竖线上。
    const newConversation = page.getByRole("button", { name: "新会话", exact: true });
    const settings = page.getByRole("button", { name: "系统配置", exact: true });
    await settings.hover();
    await expect.poll(() => settings.evaluate((element) => getComputedStyle(element).backgroundColor)).toBe(hoverColor);
    expect((await box(newConversation.locator("svg"))).x).toBeCloseTo((await box(workspace.locator("svg").first())).x, 0);
    // 侧栏开关的 aria-expanded 表示侧栏展开，不是菜单展开：平时不带底色。
    await expect(page.getByRole("button", { name: "切换侧栏" })).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    // 侧栏里的图标按钮悬停同样看得见。
    const toggle = page.getByRole("button", { name: "切换侧栏" });
    await toggle.hover();
    await expect.poll(() => toggle.evaluate((element) => getComputedStyle(element).backgroundColor)).toBe(hoverColor);

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

    await newConversation.click();
    // 新会话页（背后是工作区未开始的会话，不在侧栏里）在侧栏上表现为工作区被选中，「新会话」是当前项，
    // 底色与当前会话相同。
    await expect(page.getByRole("heading", { name: /想做什么/ })).toBeVisible();
    await expect(newConversation).toHaveAttribute("aria-current", "page");
    await page.mouse.move(800, 400);
    await expect.poll(() => newConversation.evaluate((element) => getComputedStyle(element).backgroundColor)).toBe(selectedColor);
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
    await page.getByRole("tree", { name: "工作区和会话列表" }).focus();
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
    await expect(page).toHaveURL(/\/sessions\/100$/);
    await expect(selected).toHaveAttribute("aria-current", "page");
    await expect(page.getByLabel("消息输入")).toBeVisible();
    await expect(selected).toBeFocused();

    await page.keyboard.press("Tab");
    const menuButton = selected.getByRole("button", { name: `更多操作：${longTitle}`, exact: true });
    await expect(menuButton).toBeFocused();
    await page.keyboard.press("Space");
    await expect(page.getByRole("menuitem", { name: "置顶" })).toBeFocused();
    await page.keyboard.press("ArrowDown");
    // radix 在下一个会话里才移动菜单焦点，确认落到「删除」再按 Enter。
    await expect(page.getByRole("menuitem", { name: "删除" })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("heading", { name: `删除会话「${longTitle}」？` })).toBeVisible();
    // 它有一个 fork：询问只删这个还是一起删。
    await expect(page.getByRole("button", { name: "只删除这个", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "一起删除 2 个会话", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(menuButton).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "系统配置", exact: true })).toBeFocused();
    await expect(page).toHaveURL(/\/sessions\/100$/);

    await page.getByRole("tree").focus();
    await page.keyboard.press("ArrowDown");
    await expect(row(page, "分支会话")).toBeFocused();
    await expect(selected).toHaveAttribute("aria-current", "page");
    await page.keyboard.press("Space");
    await expect(page).toHaveURL(/\/sessions\/102$/);
  });
}

test("点工作区行只展开收起、不改选中工作区；「+」在该工作区开始新会话", async ({ page, request }) => {
  await seedSidebar(page, request, "light");
  await page.goto("/sessions/100");
  const other = row(page, "其他工作区");
  await other.click();
  await expect(page).toHaveURL(/\/sessions\/100$/);
  // 行点击不再悄悄改写选中工作区：回到新会话页仍是上次的工作区。
  await page.getByRole("button", { name: "新会话", exact: true }).click();
  await expect(page.getByRole("button", { name: `选择工作区，当前 ${workspaceName}` })).toBeVisible();

  await page.goto("/sessions/100");
  await other.hover();
  await other.getByRole("button", { name: "在 其他工作区 中新建会话" }).click();
  await expect(page).toHaveURL(`${WEB_ORIGIN}/`);
  await expect(page.getByRole("button", { name: "选择工作区，当前 其他工作区" })).toBeVisible();
});

test("Fork 挂在源会话下：缩进一层，可点击或用方向键折叠", async ({ page, request }) => {
  await seedSidebar(page, request, "light");
  await page.goto("/");
  const source = row(page, longTitle);
  const forked = row(page, "分支会话");
  await expect(source).toHaveAttribute("aria-level", "2");
  await expect(forked).toHaveAttribute("aria-level", "3");
  // 嵌套的 Fork 由层级表达身份，不再重复显示 Fork 图标。
  await expect(forked.getByRole("img", { name: "Fork 会话" })).toHaveCount(0);
  expect((await box(forked.locator(".workspace-tree-title"))).x).toBeGreaterThan((await box(source.locator(".workspace-tree-title"))).x);

  const toggle = source.getByRole("button", { name: `收起 ${longTitle} 的 Fork`, exact: true });
  await expect(page.getByRole("heading", { name: /想做什么/ })).toBeVisible();
  const newConversationUrl = page.url();
  await toggle.click();
  await expect(forked).toHaveCount(0);
  // 折叠不打开会话：仍停在新会话页。
  expect(page.url()).toBe(newConversationUrl);
  await expect(source.getByRole("button", { name: `展开 ${longTitle} 的 Fork`, exact: true })).toHaveAttribute("aria-expanded", "false");


  // 点标题打开源会话并让它成为树的焦点行，再用方向键展开 / 收起。
  await source.locator(".workspace-tree-title").click();
  await expect(page).toHaveURL(/\/sessions\/100$/);
  await expect(source).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(forked).toBeVisible();
  await page.keyboard.press("ArrowLeft");
  await expect(forked).toHaveCount(0);
});

test("置顶：顶层会话连同 Fork 移到置顶区并写入偏好，取消后回到工作区", async ({ page, request }) => {
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
  // 置顶区排在工作区之前，Fork 跟随源会话移动。
  const order = await page.getByRole("treeitem").evaluateAll((items) => items.map((item) => item.getAttribute("aria-label")));
  expect(order.slice(0, 4)).toEqual(["置顶", longTitle, "分支会话", workspaceName]);

  // 只有顶层会话可置顶，Fork 的菜单里没有置顶项。
  const forked = row(page, "分支会话");
  await forked.hover();
  await forked.getByRole("button", { name: "更多操作：分支会话", exact: true }).click();
  await expect(page.getByRole("menuitem", { name: "删除" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "置顶" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  // 夹具每次导航都会清空 localStorage，无法跨刷新验证，这里直接断言偏好已写入。
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("cosir:pinned-sessions") ?? "null")?.state?.ids)).toEqual([100]);

  await source.hover();
  await source.getByRole("button", { name: `更多操作：${longTitle}`, exact: true }).click();
  await page.getByRole("menuitem", { name: "取消置顶" }).click();
  await expect(pinnedGroup).toHaveCount(0);
  await expect(source).toHaveAttribute("aria-level", "2");
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("cosir:pinned-sessions") ?? "null")?.state?.ids)).toEqual([]);
});

test("拖拽侧栏右缘调宽：最宽时 20 字标题完整显示，宽度写入偏好", async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await seedSidebar(page, request, "light");
  const twentyChars = "检查侧栏标题在最大宽度下能否完整显示二十";
  const now = new Date().toISOString();
  const titled = { session_id: 103, workspace_id: 7, title: twentyChars, session_type: "user", created_at: now, updated_at: now };
  // 后注册的路由优先：只给这条用例追加一个 20 字标题的会话。
  await page.route(`${origin}/workspaces/7/sessions`, (route) => route.fulfill({ json: [titled] }));
  await page.route(`${origin}/sessions/103`, (route) => route.fulfill({ json: titled }));
  await seedState(request, 103, { title: twentyChars, messages: [], activity: null });
  await page.goto("/sessions/103");

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
  const titled = { session_id: 104, workspace_id: 7, title: fitting, session_type: "user", created_at: now, updated_at: now };
  await page.route(`${origin}/workspaces/7/sessions`, (route) => route.fulfill({ json: [titled] }));
  await page.route(`${origin}/sessions/104`, (route) => route.fulfill({ json: titled }));
  await seedState(request, 104, { title: fitting, messages: [], activity: null });
  await page.goto("/sessions/104");

  const target = row(page, fitting);
  const title = target.locator(".workspace-tree-title");
  await expect(title).toBeVisible();
  expect(await title.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  const before = await box(title);
  await target.hover();
  await expect(target.getByRole("button", { name: `更多操作：${fitting}`, exact: true })).toBeVisible();
  expect(await box(title)).toEqual(before);
});

/** 工作区 7 下 `count` 个顶层会话，id 越大越新（侧栏按最近更新倒序，先显示 200+count-1）。 */
async function seedManySessions(
  page: Page,
  request: APIRequestContext,
  count: number,
  statuses: Record<number, string | null> = {},
  extra: Record<number, StatusExtra> = {},
) {
  await seedSidebar(page, request, "light");
  const many = Array.from({ length: count }, (_, index) => {
    const at = new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString();
    return { session_id: 200 + index, workspace_id: 7, title: `批量会话 ${index}`, session_type: "user", created_at: at, updated_at: at };
  });
  await page.route(`${origin}/workspaces/7/sessions`, (route) => route.fulfill({ json: many }));
  await page.route(`${origin}/session-feed/stream`, (route) => route.fulfill({
    contentType: "application/x-ndjson", body: statusFrame(statuses, extra),
  }));
  for (const session of many) {
    await page.route(`${origin}/sessions/${session.session_id}`, (route) => route.fulfill({ json: session }));
    await seedState(request, session.session_id, { title: session.title, messages: [], activity: null });
  }
}

test("每个工作区默认只显示最近 5 个会话，可展开其余并收起", async ({ page, request }) => {
  await seedManySessions(page, request, 8);
  await page.goto("/");
  await expect(row(page, "批量会话 7")).toBeVisible();
  await expect(row(page, "批量会话 3")).toBeVisible();
  await expect(row(page, "批量会话 2")).toHaveCount(0);

  await row(page, "展开其余 3 个").click();
  await expect(row(page, "批量会话 0")).toBeVisible();
  await row(page, "收起").click();
  await expect(row(page, "批量会话 2")).toHaveCount(0);
  await expect(row(page, "展开其余 3 个")).toBeVisible();
});

test("运行中与打开过的会话留在可见范围内，离开后也不消失", async ({ page, request }) => {
  await seedManySessions(page, request, 12, { 201: "running" });
  await page.goto("/sessions/202");
  // 202 排在第 10 位：打开它把水位线推到第 10 个；201 正在运行，推到第 11 个。
  await expect(row(page, "批量会话 2")).toHaveAttribute("aria-current", "page");
  await expect(row(page, "批量会话 1")).toBeVisible();
  await expect(row(page, "批量会话 0")).toHaveCount(0);
  await expect(row(page, "展开其余 1 个")).toBeVisible();

  const newConversation = page.getByRole("button", { name: "新会话", exact: true });
  await expect(newConversation).not.toHaveAttribute("aria-current", "page");
  await newConversation.click();
  // 新会话页就是未开始的会话：`/` 会接着转到它，所以只断言已离开 202。
  await expect(page).not.toHaveURL(/\/sessions\/202$/);
  await expect(newConversation).toHaveAttribute("aria-current", "page");
  await expect(row(page, "批量会话 2")).toBeVisible();
});

test("在等你的两类分开写明，Run 已结束也不例外；页头不再显示状态", async ({ page, request }) => {
  await seedSidebar(page, request, "light", { 100: "cancelled", 101: "completed", 102: "running" }, { 101: { awaitingApprovals: 2 }, 102: { awaitingDecisions: 1 } });
  await page.goto("/sessions/100");
  const approval = row(page, "核对运行状态");
  await expect(approval.getByRole("img", { name: "待审批 · 2" })).toBeVisible();
  await expect(approval.locator(".workspace-tree-time")).toHaveText("待审批 · 2");
  await expect(approval.locator(".workspace-tree-title")).toHaveCSS("font-weight", "500");
  // 待回答先于进行中：Run 在跑也写待回答；提醒是图标时 Fork 图标让位。
  const answer = row(page, "分支会话");
  await expect(answer.getByRole("img", { name: "待回答" })).toBeVisible();
  await expect(answer.getByRole("img", { name: "Fork 会话" })).toHaveCount(0);
  // 被停下的会话不留标记；页头只剩面包屑与操作。
  await expect(row(page, longTitle).getByRole("img")).toHaveCount(0);
  const header = page.locator("header").first();
  await expect(header.getByText(/已停止|已完成|待审批|等你操作/)).toHaveCount(0);
});

test("没看过的结果标圆点并加粗，打开即告诉后端看过了；正开着的会话不标", async ({ page, request }) => {
  await seedSidebar(page, request, "light", { 100: "completed", 101: "failed", 102: "completed" }, { 100: { unread: true }, 101: { unread: true }, 102: { unread: true } });
  const read = page.waitForRequest((sent) => sent.method() === "POST" && sent.url() === `${origin}/sessions/100/read`);
  await page.goto("/sessions/100");
  await read;
  await expect(row(page, longTitle).getByRole("img")).toHaveCount(0);
  const failed = row(page, "核对运行状态");
  await expect(failed.getByRole("img", { name: "运行失败" })).toBeVisible();
  await expect(failed.locator(".workspace-tree-time")).toHaveText("运行失败");
  const unread = row(page, "分支会话");
  // 未读只用圆点与加粗，行尾仍是时间。
  await expect(unread.getByRole("img", { name: "有新结果" })).toBeVisible();
  await expect(unread.locator(".workspace-tree-title")).toHaveCSS("font-weight", "500");
  await expect(unread.locator("time")).toBeVisible();
});

test("在等你与没看过结果的会话留在可见范围内，即使它的 Run 早已结束", async ({ page, request }) => {
  await seedManySessions(page, request, 12, { 200: "completed", 201: "completed" }, { 200: { unread: true }, 201: { awaitingDecisions: 1 } });
  await page.goto("/");
  await expect(row(page, "批量会话 1")).toBeVisible();
  await expect(row(page, "批量会话 0")).toBeVisible();
});

test("搜索打开命令面板；在新会话页的工作区选择器里新建工作区后在新工作区开始新会话", async ({ page, request }) => {
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
  await page.route(`${origin}/workspaces/9/sessions`, (route) => route.fulfill({ json: [] }));
  await page.goto("/sessions/100");

  await page.getByRole("button", { name: "搜索会话", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "命令面板" })).toBeVisible();
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "新会话", exact: true }).click();
  await page.getByRole("button", { name: `选择工作区，当前 ${workspaceName}` }).click();
  await page.getByRole("button", { name: "新建工作区", exact: true }).click();
  await expect(row(page, "新项目")).toBeVisible();
  expect(created).toEqual([{ name: "新项目", root_path: "/Users/me/projects/新项目" }]);
  await expect(page).toHaveURL(`${WEB_ORIGIN}/`);
  await expect(page.getByRole("button", { name: "选择工作区，当前 新项目" })).toBeVisible();
  await expect(row(page, "新项目")).toHaveAttribute("aria-current", "true");
});
