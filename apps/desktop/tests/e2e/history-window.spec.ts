import { expect, test, type Page } from "@playwright/test";

import type { TransportState } from "@/lib/assistant/contract";

import { seedState } from "./transport-fixtures";
import { API_ORIGIN as origin } from "./e2e-env";

/**
 * 长对话的历史窗口：打开时只带最近一段（模拟后端与真实后端同为约 100 条），往上滚接近顶部时
 * 预取上一页插到前面且视口不跳。
 */
const SESSION_ID = 4501;
const PAIRS = 160;
const OUTCOME = { status: "completed" as const, endReason: "stop", error: null, usage: null, contextWindowTotal: null, durationMs: 0 };
const LAUNCH = { modelConfigId: null, reasoningEffort: null };

type SeedState = Omit<TransportState, "messageOffset" | "terminal_sessions" | "background_tasks" | "foreground_waits" | "handoffs">;

function longConversation(pairs = PAIRS): SeedState {
  const messages: TransportState["messages"] = [];
  for (let index = 0; index < pairs; index += 1) {
    messages.push({ id: `u-${index}`, role: "user", parts: [{ type: "text", text: `问题 ${index}`, status: "completed" }], turn: index + 1, inherited: false, outcome: null, source: null } as TransportState["messages"][number]);
    const parts: TransportState["messages"][number]["parts"] = [{ type: "text", text: `第 ${index} 轮回答。${"说明。".repeat(20)}`, status: "completed" }];
    messages.push({ id: `assistant-turn-${index + 1}`, role: "assistant", turn: index + 1, inherited: false, outcome: OUTCOME, source: null, parts } as TransportState["messages"][number]);
  }
  // `durationMs` 只属于结局；activity 是固定字段的白名单，多一个键整份快照就无效。
  const activity = {modelActivity: null, turn: pairs, resumable: false, status: OUTCOME.status, endReason: OUTCOME.endReason, error: null, usage: null, contextWindowTotal: null, launch: LAUNCH, startedAt: "2026-10-27T00:00:00.000Z" };
  return { title: "长对话", messages, activity };
}

const viewport = (page: Page) => page.locator(".aui-thread-root .overflow-y-auto").first();

/** 打开对话并停在底部：不依赖首次落底（曾在已移除的无头 WebKit 项目里偶发停在顶部）。 */
async function openAtBottom(page: Page): Promise<void> {
  await page.goto(`/sessions/${SESSION_ID}`);
  await expect(page.locator(".aui-thread-root [data-index]").first()).toBeVisible();
  await viewport(page).evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await expect(page.getByText(`第 ${PAIRS - 1} 轮回答`, { exact: false })).toBeVisible();
}

test.beforeEach(async ({ request, page }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId: SESSION_ID, title: "长对话" } });
  await seedState(request, SESSION_ID, longConversation());
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

test("对话区被隐藏再显示后，停在顶部的视口不跳", async ({ page, request }) => {
  // 浏览器面板铺满时对话区（main）被 display:none 隐藏；这里直接切换同一个类模拟。用全部加载得下的
  // 短对话停在顶部：顶部是隐藏期间 0 高度最容易引发连续滚动补偿的位置，且不会触发往前翻页。
  const sessionId = SESSION_ID + 1;
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId, title: "短对话" } });
  await seedState(request, sessionId, longConversation(40));
  await page.goto(`/sessions/${sessionId}`);
  await expect(page.locator(".aui-thread-root [data-index]").first()).toBeVisible();
  await viewport(page).evaluate((element) => { element.scrollTop = 0; });
  await expect(page.getByText("问题 0", { exact: true })).toBeInViewport();

  const main = page.locator("main").first();
  await main.evaluate((element) => element.classList.add("hidden"));
  await expect(main).toBeHidden();
  await main.evaluate((element) => element.classList.remove("hidden"));
  await expect(page.getByText("问题 0", { exact: true })).toBeInViewport();
  await page.waitForTimeout(300);
  expect(await viewport(page).evaluate((element) => element.scrollTop)).toBe(0);
});
