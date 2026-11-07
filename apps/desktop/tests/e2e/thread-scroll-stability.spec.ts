import { expect, test, type Page } from "@playwright/test";

import type { TransportState } from "@/lib/assistant/contract";

import { seedState } from "./transport-fixtures";
import { API_ORIGIN as origin } from "./e2e-env";

/**
 * 长对话向上滚动时，虚拟列表对 scrollTop 的被动修正量。
 *
 * 每帧把 scrollTop 往上设 60px，下一帧读回：与设定值的偏差就是虚拟列表按「实测 − 估计」做的补偿。
 * 补偿会打断 WKWebView 的惯性滚动，所以这里守住三件事：按内容估计让首次经过的补偿量小；同一次
 * 挂载里再经过时几乎不补偿（消息根节点不得叠加 content-visibility，否则每次经过都回到占位高度）；
 * 切走再回来时按消息 id 记下的实测行高仍然生效。
 */
const TASK_ID = 4201;
const OTHER_TASK_ID = 4202;
const PAIRS = 60;
const ACTIVITY_OUTCOME = { status: "completed" as const, endReason: "stop", error: null, usage: null, contextWindowTotal: null };
// `durationMs` 只属于结局；activity 是固定字段的白名单。
const OUTCOME = { ...ACTIVITY_OUTCOME, durationMs: 0 };
const LAUNCH = { modelConfigId: null, reasoningEffort: null, disabledToolGroups: [] };

function answer(index: number): string {
  const lines = [`## 第 ${index} 轮回答`, "", "这是一段较长的说明文字，用来撑开消息高度。".repeat(4 + (index % 4)), ""];
  for (let item = 0; item < 3 + (index % 5); item += 1) lines.push(`- 列表项 ${item}：${"细节描述 ".repeat(8)}`);
  lines.push("", "```ts", ...Array.from({ length: 4 + (index % 12) }, (_, line) => `const value${line} = compute(${line});`), "```", "");
  lines.push("结尾段落。".repeat(6));
  return lines.join("\n");
}

function longConversation(): TransportState {
  const messages: TransportState["messages"] = [];
  for (let index = 0; index < PAIRS; index += 1) {
    messages.push({ id: `u-${index}`, role: "user", parts: [{ type: "text", text: `问题 ${index}：${"请帮我看看。".repeat(index % 3 + 1)}` }], runId: index + 1, source: null, outcome: null });
    messages.push({
      id: `a-${index}`, role: "assistant", runId: index + 1, source: null, outcome: OUTCOME,
      parts: [
        { type: "text", text: answer(index), finalReply: true },
      ],
    } as TransportState["messages"][number]);
  }
  return {
    title: "长对话", messages, messageOffset: 0, activity: { modelActivity: null, runId: PAIRS, ...ACTIVITY_OUTCOME, launch: LAUNCH, startedAt: "2026-10-27T00:00:00.000Z" },
    terminal_sessions: [], background_tasks: [], foreground_waits: [], handoffs: [],
  };
}

type Pass = { corrections: number; correctionPx: number };

async function scrollUpThroughHistory(page: Page): Promise<Pass> {
  return page.evaluate(async () => {
    const viewport = document.querySelector<HTMLElement>(".aui-thread-root .overflow-y-auto");
    if (!viewport) throw new Error("thread viewport missing");
    const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    viewport.scrollTop = viewport.scrollHeight;
    for (let index = 0; index < 20; index += 1) await frame();
    let corrections = 0;
    let correctionPx = 0;
    let expected = viewport.scrollTop;
    for (let step = 0; step < 400 && viewport.scrollTop > 0; step += 1) {
      expected = Math.max(0, viewport.scrollTop - 60);
      viewport.scrollTop = expected;
      await frame();
      const drift = Math.abs(viewport.scrollTop - expected);
      if (drift > 1) {
        corrections += 1;
        correctionPx += drift;
      }
    }
    return { corrections, correctionPx: Math.round(correctionPx) };
  });
}

test.beforeEach(async ({ request }) => {
  await request.post(`${origin}/__test__/reset`);
});

// 目的：未测量的行穿过时总高度一直在变；潜在缺陷：平滑滚动反复重发，已结束的长对话几秒都到不了底。
test("已结束的长对话从顶部回到底部一步到位", async ({ page, request }) => {
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: TASK_ID, title: "长对话" } });
  await request.post(`${origin}/__test__/seed-state`, { data: { taskId: TASK_ID, state: longConversation() } });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(`/tasks/${TASK_ID}`);
  const viewport = page.locator(".aui-thread-root .overflow-y-auto").first();
  await expect(page.getByRole("button", { name: "回到底部", exact: true })).toBeDisabled();
  await viewport.evaluate(el => { el.scrollTop = 2000; });
  await page.getByRole("button", { name: "回到底部", exact: true }).click();
  await expect.poll(() => viewport.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight), { timeout: 1000 }).toBeLessThan(3);
});

test("运行中上翻历史保留不连续尾行之间的高度，正文增长不拖动历史视口", async ({ page, request }) => {
  const state = longConversation();
  // 保持一屏历史窗口以内，隔离分页与不连续渲染区间的行为。
  state.messages = state.messages.slice(0, 80);
  state.activity = { ...state.activity!, runId: 40, status: "running", endReason: null, startedAt: new Date().toISOString() };
  const last = state.messages.at(-1)!;
  last.outcome = null;
  last.parts = [{ type: "text", text: "正在生成的末尾回复", status: "running" }];
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: TASK_ID, title: "运行中长对话" } });
  await request.post(`${origin}/__test__/seed-state`, { data: { taskId: TASK_ID, state, hold: true } });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(`/tasks/${TASK_ID}`);
  const viewport = page.locator(".aui-thread-root .overflow-y-auto").first();
  await expect(page.getByText("正在生成的末尾回复", { exact: true })).toBeVisible();
  await viewport.evaluate(el => { el.scrollTop = 4000; });
  await expect.poll(() => page.locator('.aui-thread-root [data-index]').evaluateAll(rows => {
    const indexes = rows.map(row => Number(row.getAttribute("data-index")));
    return indexes.some((index, position) => position > 0 && index - indexes[position - 1] > 1);
  })).toBe(true);
  const geometry = await page.locator('.aui-thread-root [data-index]').evaluateAll(rows => {
    const index = rows.findIndex((row, position) => position > 0 && Number(row.getAttribute("data-index")) - Number(rows[position - 1].getAttribute("data-index")) > 1);
    const previous = rows[index - 1], next = rows[index];
    return { missingRows: Number(next.getAttribute("data-index")) - Number(previous.getAttribute("data-index")) - 1,
      gap: next.getBoundingClientRect().top - previous.getBoundingClientRect().bottom };
  });
  expect(geometry.gap).toBeGreaterThan(geometry.missingRows * 60);
  const before = await viewport.evaluate(el => el.scrollTop);
  await request.post(`${origin}/__test__/append-text`, { data: { taskId: TASK_ID, messageIndex: 79, partIndex: 0, text: "\n\n流式增长".repeat(30) } });
  await expect(page.locator('[data-index="79"]')).toContainText("流式增长");
  await expect.poll(() => viewport.evaluate(el => el.scrollTop)).toBeCloseTo(before, 0);
  // 同一交互仍能明确回到底部，且当前两条消息始终挂载。
  await expect(page.locator('[data-index="78"]')).toHaveCount(1);
  await page.getByRole("button", { name: "回到底部", exact: true }).click();
  await expect.poll(() => viewport.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)).toBeLessThan(3);
  const bottomBefore = await viewport.evaluate(el => el.scrollTop);
  await request.post(`${origin}/__test__/append-text`, { data: { taskId: TASK_ID, messageIndex: 79, partIndex: 0, text: "\n\n回到底部后的增长".repeat(30) } });
  await expect(page.locator('[data-index="79"]')).toContainText("回到底部后的增长");
  await expect.poll(() => viewport.evaluate(el => el.scrollTop)).toBeCloseTo(bottomBefore, 0);
});

test("长对话向上滚动时虚拟列表少修正 scrollTop，切走再回来沿用实测行高", async ({ page, request }) => {
  test.setTimeout(90_000);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: OTHER_TASK_ID, title: "另一个对话" } });
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: TASK_ID, title: "长对话" } });
  await seedState(request, TASK_ID, longConversation());
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(`/tasks/${TASK_ID}`);
  // 只等消息行出来，不断言打开时落在底部：曾在已移除的无头 WebKit 项目里首次落底偶发失败，
  // 测量本身会先把视口滚到底。
  const rows = page.locator(".aui-thread-root [data-index]");
  await expect(rows.first()).toBeVisible();

  const first = await scrollUpThroughHistory(page);
  const again = await scrollUpThroughHistory(page);

  await page.getByRole("treeitem").filter({ hasText: "另一个对话" }).click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${OTHER_TASK_ID}$`));
  await page.getByRole("treeitem").filter({ hasText: "长对话" }).click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${TASK_ID}$`));
  await expect(rows.first()).toBeVisible();
  const revisit = await scrollUpThroughHistory(page);

  console.log(`[thread-scroll] first=${JSON.stringify(first)} again=${JSON.stringify(again)} revisit=${JSON.stringify(revisit)}`);
  // 首次经过只剩按内容估计的误差；固定估计值（120px）时约 9000px。
  expect(first.correctionPx).toBeLessThan(3000);
  // 同一次挂载、切走再回来都用实测值；消息根或代码块叠加 content-visibility 时这里约 2000–5000px。
  expect(again.correctionPx).toBeLessThan(600);
  expect(revisit.correctionPx).toBeLessThan(300);
});
