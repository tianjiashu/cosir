import { expect, test, type Page } from "@playwright/test";

const origin = "http://127.0.0.1:8000";

test.beforeEach(async ({ request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: 42, title: "终端工作台" } });
});

const panel = (page: Page) => page.getByRole("region", { name: "终端工作台" });
const screen = (page: Page, sessionId: string) => page.locator(`[data-terminal-session="${sessionId}"] .xterm-rows`);

test("页头入口打开底部面板并自动新建终端，输入有回显", async ({ page }) => {
  await page.goto("/tasks/42");
  const entry = page.getByRole("button", { name: /^终端，/ });
  await expect(entry).toHaveAttribute("aria-pressed", "false");
  await entry.click();

  await expect(entry).toHaveAttribute("aria-pressed", "true");
  await expect(panel(page)).toBeVisible();
  // 只有一个会话：标签显示「终端」不编号，仍可关闭。
  await expect(panel(page).getByRole("tab")).toHaveText(["终端"]);
  await expect(panel(page).getByRole("button", { name: /^关闭 终端/ })).toHaveCount(1);
  await expect(screen(page, "term-1")).toContainText("term-1 $");
  await expect(entry).toContainText("1");

  // 打开面板时焦点落在终端：直接键入即可。
  await page.keyboard.type("echo hi");
  await page.keyboard.press("Enter");
  await expect(screen(page, "term-1")).toContainText("term-1 $ echo hi");
});

test("「+」新建后按序编号，切换标签不丢内容，有命令运行才确认关闭", async ({ page, request }) => {
  await page.goto("/tasks/42");
  await page.getByRole("button", { name: /^终端，/ }).click();
  await expect(screen(page, "term-1")).toContainText("term-1 $");
  await page.keyboard.type("first");

  await panel(page).getByRole("button", { name: "新建终端" }).click();
  await expect(panel(page).getByRole("tab")).toHaveText(["终端 1", "终端 2"]);
  await expect(panel(page).getByRole("tab", { name: "终端 2" })).toHaveAttribute("aria-selected", "true");
  await expect(screen(page, "term-2")).toContainText("term-2 $");
  await page.keyboard.type("second");
  await expect(screen(page, "term-2")).toContainText("second");

  await panel(page).getByRole("tab", { name: "终端 1" }).click();
  await expect(screen(page, "term-1")).toContainText("term-1 $ first");
  await panel(page).getByRole("tab", { name: "终端 2" }).click();
  await expect(screen(page, "term-2")).toContainText("second");

  // 有命令在执行：关闭前确认。
  await request.post(`${origin}/__test__/terminal/command`, { data: { taskId: 42, sessionId: "term-2", running: true } });
  await panel(page).getByRole("button", { name: "关闭 终端 2" }).click();
  const dialog = page.getByRole("alertdialog").or(page.getByRole("dialog"));
  await expect(dialog).toContainText("正在运行的程序");
  await dialog.getByRole("button", { name: "关闭终端" }).click();
  await expect(panel(page).getByRole("tab")).toHaveText(["终端"]);
  await expect(page.getByRole("button", { name: /^终端，1 个运行中会话/ })).toBeVisible();

  // 最后一个终端同样可关；空闲时不确认，直接关闭并收起面板，再次打开自动新建。
  await panel(page).getByRole("button", { name: "关闭 终端" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(panel(page)).toBeHidden();
  await page.getByRole("button", { name: /^终端，/ }).click();
  await expect(panel(page).getByRole("tab")).toHaveText(["终端"]);
  await expect(screen(page, "term-3")).toContainText("term-3 $");
});

test("关闭面板只收起：会话保持，重新打开画面仍在，不再新建", async ({ page }) => {
  await page.goto("/tasks/42");
  const entry = page.getByRole("button", { name: /^终端，/ });
  await entry.click();
  await expect(screen(page, "term-1")).toContainText("term-1 $");
  await page.keyboard.type("keep me");

  await panel(page).getByRole("button", { name: "关闭终端面板" }).click();
  await expect(panel(page)).toBeHidden();
  await expect(entry).toHaveAttribute("aria-pressed", "false");

  await page.keyboard.press("Control+Backquote");
  await expect(panel(page)).toBeVisible();
  await expect(panel(page).getByRole("tab")).toHaveText(["终端"]);
  await expect(screen(page, "term-1")).toContainText("keep me");
  await expect(page.locator("[data-terminal-session]")).toHaveCount(1);
});

test("已结束会话只在查看时挂载视图，切走即释放", async ({ page }) => {
  await page.goto("/tasks/42");
  await page.getByRole("button", { name: /^终端，/ }).click();
  await expect(screen(page, "term-1")).toContainText("term-1 $");
  await panel(page).getByRole("button", { name: "新建终端" }).click();
  await expect(screen(page, "term-2")).toContainText("term-2 $");
  await page.keyboard.type("bye");
  await expect(screen(page, "term-2")).toContainText("bye");

  await panel(page).getByRole("button", { name: "关闭 终端 2" }).click();
  await expect(panel(page).getByRole("tab")).toHaveText(["终端"]);
  // 用户关闭的会话不再查看：xterm 与连接随即释放。
  await expect(page.locator("[data-terminal-session]")).toHaveCount(1);

  await panel(page).getByRole("button", { name: /^已结束的终端/ }).click();
  const endedItem = page.getByRole("menuitem").filter({ hasText: "已关闭" });
  await endedItem.click();
  await expect(screen(page, "term-2")).toContainText("bye");
  await expect(page.locator("[data-terminal-session]")).toHaveCount(2);

  await panel(page).getByRole("tab", { name: "终端 1" }).click();
  await expect(page.locator("[data-terminal-session]")).toHaveCount(1);
  await expect(page.locator('[data-terminal-session="term-1"]')).toHaveCount(1);
});

test("会话在后端失效时停止重连并平静提示，snapshot 移除后释放视图", async ({ page, request }) => {
  await page.goto("/tasks/42");
  await page.getByRole("button", { name: /^终端，/ }).click();
  await expect(screen(page, "term-1")).toContainText("term-1 $");

  await request.post(`${origin}/__test__/terminal/expire`, { data: { taskId: 42 } });
  await expect(panel(page).getByTestId("terminal-status")).toContainText("已不在后端");
  await expect(panel(page).getByTestId("terminal-status").getByRole("button", { name: "新建终端" })).toBeVisible();
  await expect(panel(page).locator(".text-destructive")).toHaveCount(0);

  await request.post(`${origin}/__test__/terminal/sync`, { data: { taskId: 42 } });
  await expect(panel(page).getByRole("tab")).toHaveCount(0);
  await expect(page.locator("[data-terminal-session]")).toHaveCount(0);
  await expect(panel(page)).toContainText("没有运行中的终端");
});

test("「+」旁的下拉选 shell 新建，非默认 shell 在标签上标出", async ({ page }) => {
  await page.goto("/tasks/42");
  await page.getByRole("button", { name: /^终端，/ }).click();
  await expect(screen(page, "term-1")).toContainText("term-1 $");

  await panel(page).getByRole("button", { name: "选择 shell" }).click();
  await expect(page.getByRole("menuitem", { name: /sh.*功能受限/ })).toBeVisible();
  await page.getByRole("menuitem", { name: /^bash/ }).click();
  await expect(panel(page).getByRole("tab")).toHaveText(["终端 1", "终端 2bash"]);
  await expect(panel(page).getByRole("tab", { name: /终端 2/ })).toHaveAttribute("aria-selected", "true");
});

test("搜索只跟随可见会话：切换标签或收起面板即关闭", async ({ page }) => {
  await page.goto("/tasks/42");
  await page.getByRole("button", { name: /^终端，/ }).click();
  await expect(screen(page, "term-1")).toContainText("term-1 $");
  const search = panel(page).getByRole("search");

  await page.keyboard.press("ControlOrMeta+f");
  await expect(search).toBeVisible();
  await search.getByRole("textbox", { name: "在终端中搜索" }).fill("term");

  // 新建并切到另一个会话：搜索不跟过去。
  await panel(page).getByRole("button", { name: "新建终端" }).click();
  await expect(panel(page).getByRole("tab", { name: "终端 2" })).toHaveAttribute("aria-selected", "true");
  await expect(search).toHaveCount(0);
  await panel(page).getByRole("tab", { name: "终端 1" }).click();
  await expect(search).toHaveCount(0);

  // 收起再打开面板：搜索同样已关闭。
  await page.keyboard.press("ControlOrMeta+f");
  await expect(search).toBeVisible();
  await panel(page).getByRole("button", { name: "关闭终端面板" }).click();
  await page.getByRole("button", { name: /^终端，/ }).click();
  await expect(panel(page)).toBeVisible();
  await expect(search).toHaveCount(0);
});

test("面板收起时异常退出在页头标红，打开面板即视为看过", async ({ page, request }) => {
  await page.goto("/tasks/42");
  const entry = page.getByRole("button", { name: /^终端，/ });
  const badge = entry.locator("[data-terminal-badge]");
  await entry.click();
  await expect(screen(page, "term-1")).toContainText("term-1 $");
  await panel(page).getByRole("button", { name: "新建终端" }).click();
  await expect(screen(page, "term-2")).toContainText("term-2 $");
  await panel(page).getByRole("button", { name: "关闭终端面板" }).click();
  await expect(badge).toHaveAttribute("data-terminal-badge", "none");

  // 正常退出（0）不提醒；非 0 退出标红并在入口说明是哪个终端。
  await request.post(`${origin}/__test__/terminal/exit`, { data: { taskId: 42, sessionId: "term-1", exitCode: 0 } });
  await expect(badge).toHaveText("1");
  await expect(badge).toHaveAttribute("data-terminal-badge", "none");
  await request.post(`${origin}/__test__/terminal/exit`, { data: { taskId: 42, sessionId: "term-2", exitCode: 2 } });
  await expect(badge).toHaveAttribute("data-terminal-badge", "attention");
  await expect(badge).toHaveText("!");
  await expect(entry).toHaveAccessibleName(/「终端 2」异常结束（退出码 2）/);

  // 打开面板即视为看过；没有活跃会话时照常自动新建一个，角标回到普通计数。
  await entry.click();
  await expect(screen(page, "term-3")).toContainText("term-3 $");
  await expect(badge).toHaveAttribute("data-terminal-badge", "none");
  await panel(page).getByRole("button", { name: "关闭终端面板" }).click();
  await expect(badge).toHaveText("1");
  await expect(badge).toHaveAttribute("data-terminal-badge", "none");
});

