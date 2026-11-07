import { expect, test, type Page } from "@playwright/test";

import { completedState, seedState } from "./transport-fixtures";
import { API_ORIGIN as origin } from "./e2e-env";

const TASK_ID = 62;

const rail = (page: Page) => page.getByRole("complementary", { name: "会话侧栏" });
const railButton = (page: Page) => page.getByRole("button", { name: /^会话侧栏/ });
const card = (page: Page) => page.getByRole("region", { name: "后台任务" });

test.beforeEach(async ({ page, request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: TASK_ID, title: "会话侧栏" } });
  await seedState(request, TASK_ID, completedState(1, { id: "user-1", parts: [{ type: "text", text: "跑一下构建" }] }, [
    {presentation: "activity",  type: "tool-call", awaitingUser: false, toolCallId: "call-bg", toolName: "execute_terminal", args: { command: "make watch" }, status: "pending", background: { kind: "terminal_command", label: "make watch", startedAt: "2026-10-28T08:00:00+00:00", endedAt: null } },
  ]));
  await request.post(`${origin}/__test__/seed-background-tasks`, {
    data: {
      taskId: TASK_ID,
      entries: [{ toolCallId: "call-bg", kind: "terminal_command", label: "cd /work && make", status: "running", startedAt: new Date(Date.now() - 40_000).toISOString(), endedAt: null }],
      foregroundWaits: [],
    },
  });
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto(`/tasks/${TASK_ID}`);
  // 等种入的后台条目经 snapshot 到达（角标），之后的断言才不和首帧赛跑。
  await expect(page.getByRole("button", { name: /^会话侧栏，1 个后台任务运行中/ })).toBeVisible();
});

test("宽窗口固定为一列；打开右侧面板后浮起、不挤对话，关掉面板恢复固定", async ({ page }, testInfo) => {
  await railButton(page).click();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "docked");
  await expect(card(page)).toContainText("1 个运行中");
  await expect(card(page)).toContainText("cd /work && make");
  await expect(card(page).getByRole("button", { name: "后台任务", exact: true })).toBeInViewport({ ratio: 1 });
  await page.screenshot({ path: testInfo.outputPath("rail-docked.png") });

  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  await expect(page.getByRole("complementary", { name: "文件" })).toBeVisible();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "floating");
  await expect(card(page).getByText("cd /work && make", { exact: true })).toBeInViewport({ ratio: 1 });
  await page.screenshot({ path: testInfo.outputPath("rail-floating.png") });

  await page.getByRole("button", { name: "收起文件", exact: true }).click();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "docked");
});

// 目的：侧栏是对话画布上的附注；潜在缺陷：侧栏另占一整列时，滚动条竖在对话与侧栏之间、底部终端被它挤窄。
test("固定的侧栏在对话视口里：靠视口最右侧，底部终端延伸到侧栏下方", async ({ page }, testInfo) => {
  await railButton(page).click();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "docked");
  const thread = page.locator(".aui-thread-root");
  await expect(thread.getByRole("complementary", { name: "会话侧栏" })).toBeVisible();
  const threadBox = (await thread.boundingBox())!;
  const railBox = (await rail(page).boundingBox())!;
  // 侧栏贴着视口右缘（只隔自身内边距与可能的滚动条），视口的滚动条因此在侧栏外侧。
  expect(threadBox.x + threadBox.width - (railBox.x + railBox.width)).toBeLessThan(24);

  await page.getByRole("button", { name: /^终端，/ }).click();
  const terminal = page.getByRole("region", { name: "终端工作台" });
  await expect(terminal).toBeVisible();
  const terminalBox = (await terminal.boundingBox())!;
  expect(terminalBox.x + terminalBox.width).toBeGreaterThan(railBox.x + railBox.width - 24);
  await page.screenshot({ path: testInfo.outputPath("rail-docked-with-terminal.png") });
});

test("浮层非模态：点对话区不关闭，焦点在侧栏内按 Esc 关闭", async ({ page }) => {
  await page.setViewportSize({ width: 1100, height: 900 });
  await railButton(page).click();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "floating");
  // 浮层盖住的是对话区右上角，点左侧空白处的对话区。
  const thread = (await page.getByRole("main").boundingBox())!;
  await page.mouse.click(thread.x + 40, thread.y + thread.height / 2);
  await expect(rail(page)).toBeVisible();
  await card(page).getByRole("button", { name: "后台任务", exact: true }).focus();
  await page.keyboard.press("Escape");
  await expect(rail(page)).toHaveCount(0);
  await expect(railButton(page)).toHaveAttribute("aria-pressed", "false");
  // 焦点原本在已卸载的侧栏里，交还给页头按钮而不是落到 body。
  await expect(railButton(page)).toBeFocused();
});

test("开关与折叠是全局偏好，刷新后保留；折叠时摘要仍在", async ({ page }) => {
  await railButton(page).click();
  await card(page).getByRole("button", { name: "后台任务", exact: true }).click();
  await expect(card(page)).not.toContainText("cd /work && make");
  await expect(card(page)).toContainText("1 个运行中");
  await page.reload();
  await expect(rail(page)).toBeVisible();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "docked");
  await expect(card(page).getByRole("button", { name: "后台任务", exact: true })).toBeInViewport({ ratio: 1 });
  await expect(card(page)).toContainText("1 个运行中");
  await expect(card(page)).not.toContainText("cd /work && make");
});

// 侧栏开关跨 Task 保留，视口却随 Task 重建：新视口必须重新建立尺寸订阅。
test("两栏打开时切换会话：卡片完整可见，仍能随文件栏切换摆放", async ({ page, request }) => {
  const nextTaskId = TASK_ID + 1;
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: nextTaskId, title: "另一个会话" } });
  await page.reload();
  await railButton(page).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  await page.getByText("另一个会话", { exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${nextTaskId}$`));
  await expect(rail(page)).toHaveAttribute("data-task-rail", "floating");
  await expect(card(page).getByRole("button", { name: "后台任务", exact: true })).toBeInViewport({ ratio: 1 });
  await expect(card(page).getByText("没有后台任务", { exact: true })).toBeInViewport({ ratio: 1 });

  await page.getByRole("button", { name: "收起文件", exact: true }).click();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "docked");
  await expect(card(page).getByText("没有后台任务", { exact: true })).toBeInViewport({ ratio: 1 });
});

test("刷新后展开的侧栏响应视口宽高，文件面板铺满再还原不丢失订阅", async ({ page }) => {
  await railButton(page).click();
  await page.reload();
  const label = card(page).getByText("cd /work && make", { exact: true });
  await expect(rail(page)).toHaveAttribute("data-task-rail", "docked");
  await expect(label).toBeInViewport({ ratio: 1 });

  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "floating");
  await expect(label).toBeInViewport({ ratio: 1 });
  await page.getByRole("button", { name: "铺满文件面板" }).click();
  await expect(rail(page)).toBeHidden();
  await page.getByRole("button", { name: "还原文件面板" }).click();
  await expect(label).toBeInViewport({ ratio: 1 });
  await expect(rail(page)).toHaveAttribute("data-task-rail", "floating");

  const previousMaxHeight = await rail(page).evaluate((element) => parseFloat(getComputedStyle(element).maxHeight));
  await page.setViewportSize({ width: 1600, height: 700 });
  await expect.poll(() => rail(page).evaluate((element) => parseFloat(getComputedStyle(element).maxHeight))).toBeLessThan(previousMaxHeight);
  await expect(label).toBeInViewport({ ratio: 1 });
  await page.getByRole("button", { name: "收起文件", exact: true }).click();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "docked");
});

test("渲染错误恢复重建视口后侧栏恢复，尺寸更新不重绘整个线程", async ({ page, request }) => {
  const nextTaskId = TASK_ID + 1;
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: nextTaskId, title: "恢复会话" } });
  await page.reload();
  await railButton(page).click();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  // 只在测试浏览器中包装 memo 的渲染函数，触发真实 runtime 错误边界；不添加生产故障开关。
  await page.evaluate(async () => {
    const modulePath = "/components/assistant-ui/elements/thread.aui.tsx";
    const { Thread } = await import(/* @vite-ignore */ modulePath);
    const render = Thread.type;
    const probe = { fail: true, renders: 0 };
    Object.assign(window, { railThreadProbe: probe });
    Thread.type = (...args: unknown[]) => {
      probe.renders++;
      if (probe.fail) throw new Error("task rail viewport recovery probe");
      return render(...args);
    };
  });
  await page.getByText("恢复会话", { exact: true }).click();
  await expect(page.getByRole("heading", { name: "对话界面渲染失败" })).toBeVisible();
  await page.evaluate(() => { (window as unknown as { railThreadProbe: { fail: boolean } }).railThreadProbe.fail = false; });
  await Promise.all([
    page.waitForResponse(`${origin}/tasks/${nextTaskId}/assistant/state`),
    page.getByRole("button", { name: "重试", exact: true }).click(),
  ]);
  await expect(card(page).getByText("没有后台任务", { exact: true })).toBeInViewport({ ratio: 1 });
  await expect(rail(page)).toHaveAttribute("data-task-rail", "floating");
  // 等恢复提交落定再计数；只改变线程局部高度，隔离 AppLayout 自身的窗口尺寸订阅。
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => {
    (window as unknown as { railThreadProbe: { renders: number } }).railThreadProbe.renders = 0;
    resolve();
  }))));
  const previousMaxHeight = await rail(page).evaluate((element) => parseFloat(getComputedStyle(element).maxHeight));
  await page.locator(".aui-thread-root").evaluate((element) => {
    (element as HTMLElement).style.height = `${element.clientHeight - 200}px`;
  });
  await expect.poll(() => rail(page).evaluate((element) => parseFloat(getComputedStyle(element).maxHeight))).toBeLessThan(previousMaxHeight);
  expect(await page.evaluate(() => (window as unknown as { railThreadProbe: { renders: number } }).railThreadProbe.renders)).toBe(0);
});

test("只能按手柄拖动，键盘可拿起与取消；浮层里拖动中的 Esc 只取消拖动、不关闭侧栏", async ({ page }) => {
  await page.setViewportSize({ width: 1100, height: 900 });
  await railButton(page).click();
  await expect(rail(page)).toHaveAttribute("data-task-rail", "floating");
  const handle = card(page).getByRole("button", { name: "拖动排序：后台任务" });
  await handle.focus();
  await page.keyboard.press("Space");
  await expect(handle).toHaveAttribute("aria-pressed", "true");
  // dnd-kit 的键盘传感器在拿起后的下一轮事件循环才挂上按键监听，人手不会快到这一步。
  await page.waitForTimeout(200);
  await page.keyboard.press("Escape");
  await expect(handle).not.toHaveAttribute("aria-pressed", "true");
  await expect(rail(page)).toBeVisible();
});

test("配置页隐藏侧栏与按钮，回到对话后恢复", async ({ page }) => {
  await railButton(page).click();
  await expect(rail(page)).toBeVisible();
  await page.getByRole("button", { name: "系统配置", exact: true }).click();
  await expect(page).toHaveURL(/\/settings/);
  await expect(rail(page)).toHaveCount(0);
  await expect(railButton(page)).toHaveCount(0);
  await page.goBack();
  await expect(rail(page)).toBeVisible();
});

test("快捷键切换会话侧栏", async ({ page }) => {
  const modifier = await page.evaluate(() => /mac/i.test(navigator.userAgent) ? "Meta" : "Control");
  await page.locator("body").click({ position: { x: 600, y: 400 } });
  await page.keyboard.press(`${modifier}+Alt+KeyR`);
  await expect(rail(page)).toBeVisible();
  await page.keyboard.press(`${modifier}+Alt+KeyR`);
  await expect(rail(page)).toHaveCount(0);
});

test("右侧面板把对话区压窄时，输入框底栏按自身宽度让位，中文状态不竖排", async ({ page }, testInfo) => {
  await page.route(`${origin}/model-configs`, route => route.fulfill({ json: [] }));
  await page.setViewportSize({ width: 1100, height: 900 });
  await page.reload();
  await page.getByRole("button", { name: "浏览工作区文件" }).click();
  await expect(page.getByRole("complementary", { name: "文件" })).toBeVisible();
  // 窗口仍宽于按窗口判断的断点，只有输入框自己变窄：底栏要按输入框宽度收窄。
  const empty = page.getByText("暂无可用模型", { exact: true });
  await expect(empty).toBeVisible();
  expect((await empty.boundingBox())!.height).toBeLessThan(24);
  const settings = page.getByRole("button", { name: "模型设置", exact: true });
  await expect(settings).toBeVisible();
  await expect(settings.getByText("模型设置")).toBeHidden();
  await page.screenshot({ path: testInfo.outputPath("narrow-composer.png") });

  // 收起面板、输入框变宽后文字回来。
  await page.getByRole("button", { name: "收起文件", exact: true }).click();
  await expect(settings.getByText("模型设置")).toBeVisible();
});
