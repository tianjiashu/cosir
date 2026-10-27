import { expect, test, type APIRequestContext, type Page, type Request } from "@playwright/test";

import type { TransportState } from "../../lib/assistant/contract";
import { seedState } from "./transport-fixtures";

/**
 * 运行中的提交开关：活动 Run 期间照常能发送新消息、能保存排队消息的编辑，按钮与回车一致。
 *
 * assistant-ui 的 React 层在 `thread.isRunning && !capabilities.queue` 时会禁用发送；本应用的排队在
 * 后端，能否提交由应用自己判定（`useComposerSubmit`）。活动用 `suspended`：它同样是活动态，模拟
 * 后端不会自行续跑，页面能稳定停在「运行中」。
 */

const origin = "http://127.0.0.1:8000";
const TASK_ID = 42;
// 活动 Run 冻结的发起配置：运行中发送沿用它，而不是 composer 里的选择。
const RUN_LAUNCH = { modelConfigId: 2, reasoningEffort: null, banTools: ["web_search"] };

function activeState(queued: TransportState["queued_messages"] = []): TransportState {
  return {
    title: "测试对话",
    messages: [
      { id: "u-1", role: "user", runId: 1, outcome: null,
        parts: [{ type: "text", text: "第一个问题", status: "completed" }] },
      { id: "assistant-u-1", role: "assistant", runId: 1, outcome: null,
        parts: [{ type: "text", text: "正在处理", status: "running" }] },
    ],
    activity: {
      runId: 1, status: "suspended", endReason: null, error: null, usage: null,
      contextWindowTotal: null, launch: RUN_LAUNCH,
    },
    queued_messages: queued,
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
  test(`运行中可以发送新消息（${via === "button" ? "按钮" : "回车"}），沿用冻结配置与投递方式`, async ({ page, request }) => {
    await openActiveTask(page, request, activeState());
    const input = page.getByLabel("消息输入");
    await input.fill("运行中追加的消息");

    const sent = page.waitForRequest(isCommand(/^\/tasks\/42\/messages$/, "POST"));
    if (via === "button") {
      // 运行中的发送按钮按投递方式标注；默认是「立即」。
      await page.getByRole("button", { name: "立即发送", exact: true }).click();
    } else {
      await input.press("Enter");
    }

    expect((await sent).postDataJSON()).toMatchObject({
      message: { parts: [{ type: "text", text: "运行中追加的消息" }] },
      delivery: "immediate",
      modelConfigId: 2,
      banTools: ["web_search"],
    });
  });
}

test("运行中切换为「排队」后发送，请求带上 next_run 投递方式", async ({ page, request }) => {
  await openActiveTask(page, request, activeState());
  // 草稿为空时这里是停止按钮，投递方式开关随草稿出现。
  await page.getByLabel("消息输入").fill("交给下一轮");
  await page.getByRole("button", { name: "发送方式：立即", exact: true }).click();
  await expect(page.getByRole("button", { name: "加入队列", exact: true })).toBeVisible();

  const sent = page.waitForRequest(isCommand(/^\/tasks\/42\/messages$/, "POST"));
  await page.getByLabel("消息输入").press("Enter");

  expect((await sent).postDataJSON()).toMatchObject({ delivery: "next_run" });
});

for (const via of ["button", "enter"] as const) {
  test(`运行中可以保存排队消息的编辑（${via === "button" ? "按钮" : "回车"}）`, async ({ page, request }) => {
    await openActiveTask(page, request, activeState([
      { id: "q-1", role: "user", delivery: "next_run",
        parts: [{ type: "text", text: "排队中的消息", status: "completed" }] },
    ]));
    const queued = page.locator('[data-role="user"]').filter({ hasText: "排队中的消息" });
    await expect(queued.getByText("下一轮处理", { exact: true })).toBeVisible();
    await queued.hover();
    await queued.getByRole("button", { name: "编辑", exact: true }).click();

    const editInput = page.getByRole("textbox", { name: "编辑消息" });
    await expect(editInput).toHaveText("排队中的消息");
    await editInput.fill("改过的排队消息");
    // 排队编辑沿用发送时的配置，不显示模型与工具控件。
    await expect(page.getByText("排队消息沿用发送时的配置", { exact: true })).toBeVisible();

    const saved = page.waitForRequest(isCommand(/^\/tasks\/42\/queued-messages\/q-1$/, "PATCH"));
    if (via === "button") {
      await page.getByRole("button", { name: "保存", exact: true }).click();
    } else {
      await editInput.press("Enter");
    }

    // 请求体只带正文，不带配置与投递方式；保存后排队消息显示新内容。
    expect((await saved).postDataJSON()).toEqual({
      message: { role: "user", parts: [{ type: "text", text: "改过的排队消息" }] },
    });
    await expect(page.locator('[data-role="user"]').getByText("改过的排队消息", { exact: true })).toBeVisible();
  });
}

test("排队消息在编辑期间被并入：提示原因并禁止提交，回车也不会发出请求", async ({ page, request }) => {
  const queuedMessage = {
    id: "q-1", role: "user" as const, delivery: "immediate" as const,
    parts: [{ type: "text" as const, text: "排队中的消息", status: "completed" as const }],
  };
  await openActiveTask(page, request, activeState([queuedMessage]));
  const queued = page.locator('[data-role="user"]').filter({ hasText: "排队中的消息" });
  await queued.hover();
  await queued.getByRole("button", { name: "编辑", exact: true }).click();
  const editInput = page.getByRole("textbox", { name: "编辑消息" });
  await expect(editInput).toHaveText("排队中的消息");

  // 后端把它并入当前 Run：排队投影里没有了，消息进入对话，Run 仍在运行。
  const consumed = activeState();
  consumed.messages.push({ ...queuedMessage, runId: 1, outcome: null });
  delete (consumed.messages.at(-1) as { delivery?: string }).delivery;
  await seedState(request, TASK_ID, consumed);

  await expect(page.getByText("这条消息已开始处理，无法再修改。", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "保存", exact: true })).toBeDisabled();
  // 它所在的 Run 还在运行，此刻也不能改为编辑并重跑。
  await expect(page.getByRole("button", { name: "改为编辑并重跑", exact: true })).toHaveCount(0);

  const commands: string[] = [];
  page.on("request", (req) => {
    if (["PATCH", "POST"].includes(req.method()) && /\/tasks\/42\/(queued-messages|messages)/.test(req.url())) {
      commands.push(`${req.method()} ${new URL(req.url()).pathname}`);
    }
  });
  await editInput.fill("来不及的修改");
  await editInput.press("Enter");
  await page.waitForTimeout(300);
  expect(commands).toEqual([]);
});
