import { expect, test, type Page } from "@playwright/test";

import type { BackgroundTaskEntry, TransportState } from "@/lib/assistant/contract";

import { seedState } from "./transport-fixtures";

/**
 * 长对话的历史窗口：打开时只带最近一段（模拟后端与真实后端同为约 100 条），往上滚接近顶部时
 * 预取上一页插到前面且视口不跳；看板「在对话中定位」能翻到尚未加载的卡片。
 */
const origin = "http://127.0.0.1:8000";
const TASK_ID = 4501;
const PAIRS = 160;
const OUTCOME = { status: "completed" as const, endReason: "stop", error: null, usage: null, contextWindowTotal: null };
const LAUNCH = { modelConfigId: null, reasoningEffort: null, disabledToolGroups: [] };

type SeedState = Omit<TransportState, "messageOffset" | "terminal_sessions" | "background_tasks" | "foreground_waits">;

function longConversation(): SeedState {
  const messages: TransportState["messages"] = [];
  for (let index = 0; index < PAIRS; index += 1) {
    messages.push({ id: `u-${index}`, role: "user", parts: [{ type: "text", text: `问题 ${index}`, status: "completed" }], runId: index + 1, outcome: null } as TransportState["messages"][number]);
    const parts: TransportState["messages"][number]["parts"] = [{ type: "text", text: `第 ${index} 轮回答。${"说明。".repeat(20)}`, status: "completed" }];
    // 第 3 轮带一张后台命令卡片：远在最近窗口之外。
    if (index === 3) parts.unshift({ type: "tool-call", toolCallId: "call-old", toolName: "terminal_read", args: {}, status: "pending", deferred: true } as never);
    messages.push({ id: `a-${index}`, role: "assistant", runId: index + 1, outcome: OUTCOME, parts } as TransportState["messages"][number]);
  }
  return { title: "长对话", messages, activity: { runId: PAIRS, ...OUTCOME, launch: LAUNCH } };
}

const viewport = (page: Page) => page.locator(".aui-thread-root .overflow-y-auto").first();

/** 打开对话并停在底部：不依赖首次落底（无头 WebKit 偶发停在顶部，改动前同样存在）。 */
async function openAtBottom(page: Page): Promise<void> {
  await page.goto(`/tasks/${TASK_ID}`);
  await expect(page.locator(".aui-thread-root [data-index]").first()).toBeVisible();
  await viewport(page).evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await expect(page.getByText(`第 ${PAIRS - 1} 轮回答`, { exact: false })).toBeVisible();
}

test.beforeEach(async ({ request, page }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: TASK_ID, title: "长对话" } });
  await seedState(request, TASK_ID, longConversation());
  await page.setViewportSize({ width: 1280, height: 900 });
});

test("打开只带最近一段，往上滚逐页加载，插入时视口不跳", async ({ page }) => {
  await openAtBottom(page);
  await expect(page.getByText("第 0 轮回答", { exact: false })).toHaveCount(0);

  // 一路滚到已加载部分的顶端：每次都触发预取，一页页往前加载，直到最早的一轮出现。
  for (let step = 0; step < 60; step += 1) {
    if (await page.getByText("第 0 轮回答", { exact: false }).count()) break;
    await viewport(page).evaluate((element) => { element.scrollTop = 0; });
    await page.waitForTimeout(150);
  }
  await expect(page.getByText("第 0 轮回答", { exact: false })).toBeVisible();
});

test("插入上一页后，视口里的内容停在原处", async ({ page }) => {
  await openAtBottom(page);
  // 让翻页响应晚到：滚到顶端附近触发预取后，先在请求在途时记下第一条可见消息的位置，
  // 再等上一页插到前面（内容总高随之增长）后比较。
  await page.route("**/assistant/messages?*", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 800));
    await route.continue();
  });
  const heightBefore = await viewport(page).evaluate((element) => element.scrollHeight);
  await viewport(page).evaluate((element) => { element.scrollTop = 300; });
  await page.waitForTimeout(200);
  const before = await viewport(page).evaluate((element) => {
    const top = element.getBoundingClientRect().top;
    const row = [...element.querySelectorAll<HTMLElement>("[data-index]")].find((candidate) => candidate.getBoundingClientRect().bottom > top + 1);
    const round = row?.innerText.match(/第 (\d+) 轮回答|问题 (\d+)/);
    return row && round ? { label: round[0], top: row.getBoundingClientRect().top } : null;
  });
  await expect.poll(() => viewport(page).evaluate((element) => element.scrollHeight)).toBeGreaterThan(heightBefore + 1000);
  await page.waitForTimeout(300);
  const after = await viewport(page).evaluate((element, label) => {
    const row = [...element.querySelectorAll<HTMLElement>("[data-index]")].find((candidate) => candidate.innerText.includes(label));
    return row ? row.getBoundingClientRect().top : null;
  }, before!.label);
  expect(before).not.toBeNull();
  expect(after).not.toBeNull();
  expect(Math.abs(after! - before!.top)).toBeLessThan(4);
});

test("看板定位能翻到还没加载的卡片", async ({ page, request }) => {
  const entry: BackgroundTaskEntry = {
    toolCallId: "call-old", kind: "terminal_command", label: "make old", status: "running",
    startedAt: new Date(Date.now() - 40_000).toISOString(), endedAt: null,
  };
  await request.post(`${origin}/__test__/seed-background-tasks`, { data: { taskId: TASK_ID, entries: [entry], foregroundWaits: [] } });
  await openAtBottom(page);
  await expect(page.locator('[data-tool-call-id="call-old"]')).toHaveCount(0);

  await page.getByRole("button", { name: /^会话侧栏/ }).click();
  const row = page.getByRole("region", { name: "后台任务" }).locator('[data-background-task="call-old"]');
  await row.getByRole("button", { name: "在对话中定位" }).click();
  const card = page.locator('.aui-thread-root [data-tool-call-id="call-old"]');
  await expect(card).toBeVisible({ timeout: 10_000 });
  await expect(card).toBeInViewport();
});
