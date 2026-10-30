import { expect, test, type APIRequestContext, type Page, type Request } from "@playwright/test";

import type { TransportState } from "../../lib/assistant/contract";
import { seedState } from "./transport-fixtures";

/**
 * 运行中的提交开关：活动 Run 期间照常能发送新消息（后端排队）、能在队列区立即发送、编辑（放回输入框）
 * 与删除排队消息，按钮与回车一致。
 *
 * assistant-ui 的 React 层在 `thread.isRunning && !capabilities.queue` 时会禁用发送；本应用的排队在
 * 后端，能否提交由应用自己判定（`useComposerSubmit`）。活动用 `suspended`：它同样是活动态，模拟
 * 后端不会自行续跑，页面能稳定停在「运行中」。
 */

const origin = "http://127.0.0.1:8000";
const TASK_ID = 42;
// 活动 Run 冻结的发起配置：运行中发送沿用它，而不是 composer 里的选择。
const RUN_LAUNCH = { modelConfigId: 2, reasoningEffort: null, disabledToolGroups: ["联网工具"] };

function activeState(queued: TransportState["queued_messages"] = []): TransportState {
  return {
    title: "测试对话",
    messages: [
      { id: "u-1", role: "user", runId: 1, outcome: null,
        parts: [{ type: "text", text: "第一个问题", status: "completed" }] },
      { id: "assistant-u-1", role: "assistant", runId: 1, outcome: null,
        parts: [{ type: "text", text: "正在处理", status: "running" }] },
    ],
    messageOffset: 0, activity: {
      runId: 1, status: "suspended", endReason: null, error: null, usage: null,
      contextWindowTotal: null, launch: RUN_LAUNCH,
    },
    queued_messages: queued,
    terminal_sessions: [], background_tasks: [], foreground_waits: [],
  };
}

async function openActiveTask(page: Page, request: APIRequestContext, state: TransportState) {
  await seedState(request, TASK_ID, state);
  await page.goto(`/tasks/${TASK_ID}`);
  await expect(page.getByText("正在处理", { exact: true })).toBeVisible();
}

const isCommand = (path: RegExp, method: string) => (req: Request) => (
  req.method() === method && path.test(new URL(req.url()).pathname)
);

test.beforeEach(async ({ page, request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-task`, { data: { taskId: TASK_ID, title: "运行中的任务" } });
  await page.addInitScript(() => window.localStorage.clear());
});

for (const via of ["button", "enter"] as const) {
  test(`运行中可以发送新消息（${via === "button" ? "按钮" : "回车"}），沿用冻结配置、不带投递方式`, async ({ page, request }) => {
    await openActiveTask(page, request, activeState());
    const input = page.getByLabel("消息输入");
    await input.fill("运行中追加的消息");

    const sent = page.waitForRequest(isCommand(/^\/tasks\/42\/messages$/, "POST"));
    if (via === "button") {
      // 运行中发送一律排队，按钮据此标注。
      await page.getByRole("button", { name: "加入队列", exact: true }).click();
    } else {
      await input.press("Enter");
    }

    const body = (await sent).postDataJSON();
    expect(body).toMatchObject({
      message: { parts: [{ type: "text", text: "运行中追加的消息" }] },
      modelConfigId: 2,
      disabledToolGroups: ["联网工具"],
    });
    expect(body.delivery).toBeUndefined();
  });
}

test("排队消息显示在输入框上方的队列区，不在消息流里；可立即发送与删除", async ({ page, request }) => {
  await openActiveTask(page, request, activeState([
    { id: "q-1", role: "user", delivery: "next_run",
      parts: [{ type: "text", text: "第一条排队", status: "completed" }] },
    { id: "q-2", role: "user", delivery: "next_run",
      parts: [{ type: "text", text: "第二条排队", status: "completed" }] },
  ]));
  const queue = page.getByRole("region", { name: "排队消息" });
  await expect(queue.getByText("2 条排队", { exact: true })).toBeVisible();
  // 消息流里只有已并入的消息，排队条目只在队列区出现一次。
  await expect(page.getByText("第一条排队", { exact: true })).toHaveCount(1);

  const first = queue.locator('[data-role="user"]').filter({ hasText: "第一条排队" });
  await expect(first.getByText("排队中", { exact: true })).toBeVisible();
  const sendNow = page.waitForRequest(isCommand(/^\/tasks\/42\/queued-messages\/q-1\/send-now$/, "POST"));
  await first.getByRole("button", { name: "立即发送", exact: true }).click();
  await sendNow;
  // 改为立即处理后以新身份重新入队，标注随之变化，不再提供立即发送。
  const promoted = queue.locator('[data-role="user"]').filter({ hasText: "第一条排队" });
  await expect(promoted.getByText("即将处理", { exact: true })).toBeVisible();
  await expect(promoted.getByRole("button", { name: "立即发送", exact: true })).toHaveCount(0);

  const second = queue.locator('[data-role="user"]').filter({ hasText: "第二条排队" });
  const deleted = page.waitForRequest(isCommand(/^\/tasks\/42\/queued-messages\/q-2$/, "DELETE"));
  await second.getByRole("button", { name: "删除", exact: true }).click();
  await deleted;
  await expect(page.getByText("第二条排队", { exact: true })).toHaveCount(0);
  await expect(queue.getByText("1 条排队", { exact: true })).toBeVisible();
});

test("编辑排队消息：撤回这一条，正文放回输入框并排在已有草稿之前", async ({ page, request }) => {
  await openActiveTask(page, request, activeState([
    { id: "q-1", role: "user", delivery: "next_run",
      parts: [{ type: "text", text: "排队中的消息", status: "completed" }] },
  ]));
  const input = page.getByLabel("消息输入");
  await input.fill("还没发的草稿");
  const queue = page.getByRole("region", { name: "排队消息" });
  const queued = queue.locator('[data-role="user"]').filter({ hasText: "排队中的消息" });
  await queued.hover();

  const withdrawn = page.waitForRequest(isCommand(/^\/tasks\/42\/queued-messages\/q-1$/, "DELETE"));
  await queued.getByRole("button", { name: "编辑", exact: true }).click();
  await withdrawn;

  // 条目离开队列，内容回到输入框；不再有原地编辑框。
  await expect(queue).toHaveCount(0);
  // 编辑器按段落渲染，textContent 不含段间换行。
  await expect(input).toHaveText(/排队中的消息\s*还没发的草稿/);
  await expect(page.getByRole("textbox", { name: "编辑消息" })).toHaveCount(0);
});
