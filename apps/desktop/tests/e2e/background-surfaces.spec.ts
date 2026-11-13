import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { API_ORIGIN as origin } from "./e2e-env";
import { completedState, seedState } from "./transport-fixtures";

const SESSION = 42;
const started = new Date(Date.now() - 40_000).toISOString();
const user = { id: "user-1", parts: [{ type: "text" as const, text: "跑一下" }] };
const running = { toolCallId: "bg", kind: "terminal_command", label: "make watch", status: "running", startedAt: started, endedAt: null };

test.beforeEach(async ({ page, request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId: SESSION, title: "甲会话" } });
  await page.setViewportSize({ width: 1600, height: 900 });
});

async function seedCommand(request: APIRequestContext) {
  await seedState(request, SESSION, completedState(1, user, [{
    presentation: "activity", type: "tool-call", awaitingUser: false, toolCallId: "bg", toolName: "execute_terminal",
    args: { command: "make watch" }, status: "pending",
    background: { kind: "terminal_command", label: "make watch", startedAt: started, endedAt: null },
    display_data: { kind: "terminal-result", command: "make watch", output: "line 1\nline 2\n" },
  }]));
  await request.post(`${origin}/__test__/seed-background-tasks`, { data: { sessionId: SESSION, entries: [running], foregroundWaits: [] } });
}

async function openBoardAndGroup(page: Page) {
  await page.goto(`/sessions/${SESSION}`);
  await page.locator('[data-slot="activity-head"]').first().click();
}

test("后台命令在时间线只留一行，详情与停止只在看板", async ({ page, request }) => {
  await seedCommand(request);
  await openBoardAndGroup(page);
  const timeline = page.locator('[data-slot="turn"] [data-tool-call-id="bg"]');
  await expect(timeline.locator('[data-terminal-mode="preview"]')).toHaveCount(0);
  await expect(timeline.getByRole("button")).toHaveCount(1);
  await expect(timeline.getByRole("button", { name: /^在后台任务中查看：/ })).toHaveCount(1);
  await expect(page.locator('[data-background-task="bg"]').getByRole("button", { name: "停止" })).toHaveCount(1);
  await expect(page.locator('[data-background-task="bg"]').getByRole("button", { name: "查看输出" })).toHaveCount(0);
  await page.locator('[data-background-task="bg"]').locator("button[aria-expanded]").click();
  await page.locator('[data-background-task="bg"]').getByRole("button", { name: "查看输出" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(1);
});

test("看板条目默认收起，手动展开后跨越运行结束仍保持展开", async ({ page, request }) => {
  await seedCommand(request);
  await page.goto(`/sessions/${SESSION}`);
  const board = page.locator('[data-background-task="bg"]');
  const toggle = board.locator("button[aria-expanded]");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click();
  await expect(board.getByRole("button", { name: "查看输出" })).toBeVisible();

  await request.post(`${origin}/__test__/seed-background-tasks`, { data: { sessionId: SESSION, entries: [{ ...running, status: "completed", endedAt: new Date().toISOString() }], foregroundWaits: [] } });
  await page.getByRole("button", { name: "1 已结束" }).click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(board.getByRole("button", { name: "查看输出" })).toBeVisible();
});
