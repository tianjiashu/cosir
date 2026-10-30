import { expect, test, type Page } from "@playwright/test";

import type { TransportState } from "@/lib/assistant/contract";

import { seedState } from "./transport-fixtures";

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
const origin = "http://127.0.0.1:8000";
const PAIRS = 60;
const OUTCOME = { status: "completed" as const, endReason: "stop", error: null, usage: null, contextWindowTotal: null };
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
    messages.push({ id: `u-${index}`, role: "user", parts: [{ type: "text", text: `问题 ${index}：${"请帮我看看。".repeat(index % 3 + 1)}` }], runId: index + 1, outcome: null });
    messages.push({
      id: `a-${index}`, role: "assistant", runId: index + 1, outcome: OUTCOME,
      parts: [
        { type: "reasoning", text: "思考".repeat(40), status: "completed" },
        { type: "text", text: answer(index), status: "completed" },
      ],
    } as TransportState["messages"][number]);
  }
  return {
    title: "长对话", messages, messageOffset: 0, activity: { runId: PAIRS, ...OUTCOME, launch: LAUNCH },
    terminal_sessions: [], background_tasks: [], foreground_waits: [],
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

test("长对话向上滚动时虚拟列表少修正 scrollTop，切走再回来沿用实测行高", async ({ page, request }) => {
  test.setTimeout(90_000);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: OTHER_TASK_ID, title: "另一个对话" } });
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: TASK_ID, title: "长对话" } });
  await seedState(request, TASK_ID, longConversation());
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(`/tasks/${TASK_ID}`);
  // 只等消息行出来，不断言打开时落在底部：无头 WebKit 下首次落底偶发失败（改动前同样存在），
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
