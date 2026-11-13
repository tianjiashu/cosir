import { expect, test } from "@playwright/test";

import type { TransportState } from "../../lib/assistant/contract";
import { seedState } from "./transport-fixtures";
import { API_ORIGIN as origin } from "./e2e-env";

const LAUNCH = { modelConfigId: null, reasoningEffort: null, disabledToolGroups: [] };

/** 主对话的一轮：Agent 调用 fork_task 分出一条侧栏对话，卡片带分叉的标题与 Task id。 */
const parentState: TransportState = {
  title: "主任务",
  messages: [
    {
      id: "1", role: "user", turn: 1, inherited: false, outcome: null, source: null,
      parts: [{ type: "text", text: "前后端一起改", status: "completed" }],
    },
    {
      id: "assistant-turn-1", role: "assistant", turn: 1, inherited: false, source: null,
      outcome: { status: "completed", endReason: "stop", error: null, usage: null, contextWindowTotal: null, durationMs: 0 },
      parts: [{presentation: "standalone",
        type: "tool-call", awaitingUser: false, toolCallId: "call-fork", toolName: "fork_task", status: "completed",
        args: { title: "前端部分", message: "只做前端" }, child_task_id: 41,
        display_data: { kind: "fork-task-result", title: "前端部分", child_task_id: 41 },
      }],
    },
  ],
  messageOffset: 0,
  activity: {modelActivity: null, turn: 1, resumable: false, status: "completed", endReason: "stop", error: null, usage: null, contextWindowTotal: null, launch: LAUNCH, startedAt: "2026-10-27T00:00:00.000Z" },
  terminal_sessions: [], background_tasks: [], foreground_waits: [], handoffs: [],
};

test.beforeEach(async ({ request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: 9, title: "主任务" } });
});

test("Agent 分出的对话：经目录失效推送进侧栏，「打开」跳到它的对话", async ({ page, request }) => {
  await page.goto("/tasks/9");
  const sidebar = page.getByRole("navigation", { name: "工作区任务列表" });
  await expect(sidebar.getByText("主任务")).toBeVisible();

  // 分叉在主对话运行中建出：页面已加载、侧栏目录里还没有它。
  await request.post(`${origin}/__test__/seed-task`, {
    data: {
      taskId: 41, title: "前端部分", taskType: "agent_fork",
      extra: { fork: { tree_parent_task_id: 9 } },
    },
  });
  await seedState(request, 9, parentState);

  await expect(sidebar.getByText("前端部分")).toBeVisible();
  await expect(sidebar.getByLabel("Agent 分出的对话")).toBeVisible();

  // 分叉调用当场完成，不再是后台工作：它是时间线上的普通调用行，「打开」就在行上。
  const row = page.locator('[data-tool-call-id="call-fork"]');
  await row.hover();
  await row.getByRole("button", { name: "打开分叉对话：前端部分" }).click();
  await expect(page).toHaveURL(/\/tasks\/41$/);
});

test("分叉已被删除：调用行留在时间线上，不反复重读目录", async ({ page, request }) => {
  let catalogReads = 0;
  page.on("request", (sent) => { if (sent.method() === "GET" && /\/workspaces\/7\/tasks$/.test(sent.url())) catalogReads++; });
  // 父对话里有 fork 行，但它指向的 Task 41 不存在（已被用户删除）。
  await seedState(request, 9, parentState);
  await page.goto("/tasks/9");
  await expect(page.locator('[data-tool-call-id="call-fork"]')).toBeAttached();
  const afterLoad = catalogReads;
  await page.waitForTimeout(1500);
  expect(catalogReads - afterLoad).toBe(0);
});

test("手动分叉的历史可读、截图可查看，但不操作来源任务", async ({ page, request }) => {
  const sourceRequests: string[] = [];
  page.on("request", request => { if (/\/tasks\/991\b/.test(request.url())) sourceRequests.push(request.url()); });
  const assetId = "a".repeat(64);
  await page.route(`${origin}/workspaces/7/attachments/${assetId}/content`, route => route.fulfill({ contentType: "image/png", body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a8n8AAAAASUVORK5CYII=", "base64") }));
  await seedState(request, 9, {
    ...parentState, activity: null,
    fork: { sourceTaskId: 991, sourceTitle: "已删除的来源", taskType: "user_fork", initialLaunch: LAUNCH },
    messages: [
      { ...parentState.messages[0], inherited: true },
      { ...parentState.messages[1], inherited: true,
        outcome: null,
        parts: [
          { type: "tool-call", presentation: "standalone", toolCallId: "history-8:child", toolName: "fork_task", status: "pending", awaitingUser: false, child_task_id: 991, args: {}, display_data: { kind: "fork-task-result", title: "来源子任务", child_task_id: 991 } },
          { type: "tool-call", presentation: "activity", toolCallId: "history-8:browser", toolName: "browser_screenshot", status: "completed", awaitingUser: false, intent: "查看已保存网页", args: {}, display_data: { kind: "browser-page", tab_id: "gone", url: "https://example.com", title: "历史页面", screenshot: assetId } },
        ] },
    ],
  });
  await page.goto("/tasks/9");
  await expect(page.locator('[data-slot="turn"][data-inherited="true"]')).toBeVisible();
  await expect(page.getByRole("button", { name: "撤回" })).toHaveCount(0);
  await page.locator('[data-slot="activity-head"]').click();
  await page.getByRole("button", { name: /查看已保存网页/ }).click();
  await expect(page.getByRole("img", { name: "网页截图", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /在浏览器面板打开|打开分叉对话|停止子任务|继续运行/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "在上下文观测中查看本回合" })).toBeVisible();
  expect(sourceRequests).toEqual([]);
});

test("继承历史共用文件、终端和错误展示，展开状态跨虚拟卸载保留", async ({ page, request }) => {
  const sourceRequests: string[] = [];
  page.on("request", req => { if (/\/tasks\/991\b/.test(req.url())) sourceRequests.push(req.url()); });
  const history: TransportState["messages"] = Array.from({ length: 100 }, (_, index) => ({
    id: `assistant-turn-${index + 1}`, role: "assistant", turn: index + 1, inherited: true, outcome: null, source: null,
    parts: [{ type: "text", status: "completed", text: `历史说明 ${index}。${"保留背景。".repeat(30)}` }],
  }));
  await seedState(request, 9, {
    ...parentState, activity: null,
    messages: [...history, { id: "assistant-turn-101", role: "assistant", turn: 101, inherited: true, outcome: null, source: null, parts: [
      { type: "text", text: "继承展示验收", status: "completed" },
      { type: "tool-call", presentation: "activity", toolCallId: "context-file", toolName: "read_file", status: "completed", awaitingUser: false,
        intent: "读取验收文件", args: { path: "src/review.py" }, display_data: { kind: "read-file-meta", path: "src/review.py", file_size: 4096, line_range: { start: 12, end: 24 }, truncated: true } },
      { type: "tool-call", presentation: "activity", toolCallId: "context-terminal", toolName: "execute_terminal", status: "failed", awaitingUser: false,
        intent: "执行验收命令", error: "失败后仍保留输出", args: { command: "printf review-output" }, display_data: { kind: "terminal-result", output: "review-output\nretained-output", command: "printf review-output", exit_code: 2 } },
      { type: "tool-call", presentation: "activity", toolCallId: "context-draft", toolName: "propose_agent_configuration", status: "completed", awaitingUser: false, args: {},
        display_data: { kind: "agent-configuration-draft", status: "draft", agent_id: "historical-helper", role: "历史角色", description: "历史说明", system_prompt: "历史指令" } },
      { type: "tool-call", presentation: "quiet", toolCallId: "context-wait", toolName: "wait_messages", status: "completed", awaitingUser: false, intent: "不应显示的协调调用", args: {} },
    ] }],
  });
  await page.goto("/tasks/9");
  const viewport = page.locator(".aui-thread-root .overflow-y-auto").first();
  await viewport.evaluate(el => { el.scrollTop = el.scrollHeight; });
  await expect(page.getByText("继承展示验收", { exact: true })).toBeVisible();
  const turn = page.locator('[data-slot="turn"]').filter({ hasText: "继承展示验收" });
  await turn.locator('[data-slot="activity-head"]').click();
  await expect(turn.getByRole("button", { name: "src/review.py", exact: true })).toBeVisible();
  await expect(turn.getByText(/L12-L24/)).toBeVisible();
  await expect(turn.getByText(/文件过大，已截断/)).toBeVisible();
  await expect(turn.getByText("失败后仍保留输出", { exact: true })).toBeVisible();
  await expect(turn.getByText("不应显示的协调调用", { exact: true })).toHaveCount(0);
  await expect(turn.getByText(/继承的配置提案/)).toBeVisible();
  await expect(turn.getByText("未保存草稿", { exact: true })).toHaveCount(0);
  await expect(turn.getByRole("button", { name: "编辑配置", exact: true })).toHaveCount(0);
  const terminal = turn.locator('[data-tool-call-id="context-terminal"]');
  await terminal.getByRole("button", { name: /执行验收命令/ }).click();
  await terminal.getByRole("button", { name: "查看输出", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("retained-output");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(terminal.getByRole("button", { name: "查看输出", exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("fork-inherited-tools.png"), animations: "disabled" });
  await viewport.evaluate(el => { el.scrollTop = 0; });
  await expect(turn).toHaveCount(0);
  await viewport.evaluate(el => { el.scrollTop = el.scrollHeight; });
  await expect(terminal.getByRole("button", { name: "查看输出", exact: true })).toBeVisible();
  await expect(turn.locator('[data-slot="activity-head"]')).toHaveAttribute("aria-expanded", "true");
  expect(sourceRequests).toEqual([]);
});
