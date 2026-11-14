import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import type { TransportState } from "../../lib/assistant/contract";
import { readState, seedState } from "./transport-fixtures";
import { API_ORIGIN as origin, OUTPUT_DIR } from "./e2e-env";

/**
 * 提问卡片（`ask_user`）：一次一题翻页、单选 / 多选、「其他」、批注、跳过，提交后收成一行；挂着卡片直接
 * 发消息，卡片变「未作答」。模拟后端与真实后端一致：决定把占位推到 completed 并带上回答，新回合让提问作废。
 */

const SESSION_ID = 42;
const CALL_ID = "call-ask";
const LAUNCH = { modelConfigId: "demo/demo-model", reasoningEffort: null, disabledToolGroups: [] };
const STORE = "登录态要保存在哪里？";
const EXPIRY = "哪些情况下要让登录态失效？";
const MIGRATE = "旧会话怎么处理？";

const QUESTIONS = [
  { question: STORE, header: "存储", multi_select: false, options: [
    { label: "HttpOnly Cookie (Recommended)", description: "后端下发，前端脚本读不到" },
    { label: "localStorage", description: "实现最快，但页面脚本可读" },
  ] },
  { question: EXPIRY, header: "过期策略", multi_select: true, options: [
    { label: "修改密码后", description: "所有设备立即退出" },
    { label: "30 天未活动", description: "滑动过期" },
  ] },
  { question: MIGRATE, header: "迁移", multi_select: false, options: [
    { label: "全部作废", description: "用户重新登录" },
    { label: "双写一周", description: "新旧并存" },
  ] },
];

function askingState(): TransportState {
  return {
    title: "测试对话",
    messages: [
      { id: "u-1", role: "user", turn: 1, inherited: false, source: null, outcome: null,
        parts: [{ type: "text", text: "把登录改成会话制", status: "completed" }] },
      { id: "assistant-turn-1", role: "assistant", turn: 1, inherited: false, source: null, outcome: null,
        parts: [
          { type: "text", text: "有三处需要你定。", status: "completed" },
          {
            type: "tool-call", toolCallId: CALL_ID, toolName: "ask_user", presentation: "standalone",
            args: { questions: QUESTIONS }, intent: "确认登录方案", status: "pending", error: null,
            awaitingUser: true, display_data: { kind: "user-question", questions: QUESTIONS },
          },
        ] },
    ],
    messageOffset: 0, activity: { modelActivity: null,
      turn: 1, resumable: false, status: "suspended", endReason: null, error: null, usage: null,
      contextWindowTotal: null, launch: LAUNCH, startedAt: "2026-10-27T00:00:00.000Z",
    },
    queued_messages: [],
    terminal_sessions: [], background_tasks: [], foreground_waits: [], handoffs: [],
  };
}

async function openQuestion(page: Page, request: APIRequestContext) {
  await seedState(request, SESSION_ID, askingState());
  await page.goto(`/sessions/${SESSION_ID}`);
  const card = page.locator('[data-slot="question-card"]');
  await expect(card).toBeVisible();
  return card;
}

async function decisions(request: APIRequestContext) {
  const telemetry = await (await request.get(`${origin}/__test__/telemetry`)).json() as { requestBodies: Array<Record<string, unknown>> };
  return telemetry.requestBodies.filter((body) => body.toolCallId === CALL_ID);
}

test.beforeEach(async ({ page, request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId: SESSION_ID, title: "提问" } });
  await page.addInitScript(() => window.localStorage.clear());
});

test("逐题作答：单选、多选加「其他」、批注、跳过，提交后收成「已作答」并可展开看问答", async ({ page, request }) => {
  const card = await openQuestion(page, request);

  // 第一题：单选。没选之前不能前进。
  await expect(card.locator('[data-slot="question-header"]')).toHaveText("存储");
  await expect(card.locator('[data-slot="question-position"]')).toHaveText("1 / 3");
  await expect(card.getByText(STORE)).toBeVisible();
  const next = card.getByRole("button", { name: /^下一题/ }).last();
  await expect(next).toBeDisabled();
  // 卡片就是「等你操作」的全部表达：下面不再另有一行状态。
  await expect(page.getByText("等待你的操作", { exact: true })).toHaveCount(0);
  await page.locator('[data-role="assistant"]').last().screenshot({ path: `${OUTPUT_DIR}/ask-user-turn.png`, animations: "disabled" });
  await card.getByLabel(/localStorage/).check();
  await card.getByLabel(/HttpOnly Cookie/).check();
  await expect(card.getByLabel(/localStorage/)).not.toBeChecked();
  // 批注默认收起，点「添加批注」才展开。
  await expect(card.getByLabel("批注", { exact: true })).toHaveCount(0);
  await card.getByRole("button", { name: "添加批注" }).click();
  await card.getByLabel("批注", { exact: true }).fill("沿用现有会话中间件");
  await next.click();

  // 第二题：多选，另填「其他」。
  await expect(card.locator('[data-slot="question-position"]')).toHaveText("2 / 3");
  await expect(card.getByText("可多选")).toBeVisible();
  await card.getByLabel(/修改密码后/).check();
  await card.getByLabel(/^其他/).check();
  await card.getByLabel("其他回答").fill("管理员强制下线");
  await page.locator('[data-slot="awaiting-card"]').screenshot({ path: `${OUTPUT_DIR}/ask-user-card.png`, animations: "disabled" });

  // 翻回上一题：草稿还在。
  await card.getByRole("button", { name: "上一题" }).click();
  await expect(card.getByLabel(/HttpOnly Cookie/)).toBeChecked();
  await expect(card.getByLabel("批注", { exact: true })).toHaveValue("沿用现有会话中间件");
  await card.getByRole("button", { name: "下一题", exact: true }).click();
  await expect(card.getByLabel("其他回答")).toHaveValue("管理员强制下线");
  await card.getByRole("button", { name: /^下一题/ }).last().click();

  // 第三题：最后一题的主按钮是「提交」；跳过它，照样提交前两题。
  await expect(card.locator('[data-slot="question-position"]')).toHaveText("3 / 3");
  await expect(card.getByRole("button", { name: /^提交/ })).toBeDisabled();
  await card.getByRole("button", { name: "跳过" }).click();

  await expect(card).toHaveCount(0);
  expect(await decisions(request)).toEqual([expect.objectContaining({
    decision: "approve",
    data: {
      answers: { [STORE]: "HttpOnly Cookie (Recommended)", [EXPIRY]: ["修改密码后", "管理员强制下线"] },
      annotations: { [STORE]: { notes: "沿用现有会话中间件" } },
    },
  })]);

  const row = page.locator(`[data-tool-call-id="${CALL_ID}"]`).filter({ has: page.locator('[data-slot="call-row"]') });
  await expect(row).toContainText("确认登录方案");
  await expect(row).toContainText("已作答");
  await expect(row).not.toContainText("等你操作");
  await row.locator('[data-slot="call-row"]').click();
  await expect(row).toContainText("修改密码后、管理员强制下线");
  await expect(row).toContainText("沿用现有会话中间件");
  await expect(row).toContainText("未回答");
  await row.screenshot({ path: `${OUTPUT_DIR}/ask-user-answered.png`, animations: "disabled" });
});

test("每题都跳过等于不回答：卡片收成「已跳过」", async ({ page, request }) => {
  const card = await openQuestion(page, request);

  for (let index = 0; index < QUESTIONS.length; index++) await card.getByRole("button", { name: "跳过" }).click();

  await expect(card).toHaveCount(0);
  expect(await decisions(request)).toEqual([expect.objectContaining({ decision: "reject" })]);
  const row = page.locator(`[data-tool-call-id="${CALL_ID}"]`).filter({ has: page.locator('[data-slot="call-row"]') });
  await expect(row).toContainText("已跳过");
});

test("挂着提问直接发消息：提问作废，卡片变「未作答」", async ({ page, request }) => {
  const card = await openQuestion(page, request);
  await card.getByLabel(/localStorage/).check();

  const input = page.getByLabel("消息输入");
  await input.fill("先别改登录，看一下报错");
  await input.press("Enter");

  await expect(card).toHaveCount(0);
  await expect(page.getByText("先别改登录，看一下报错", { exact: true })).toBeVisible();
  const state = await readState(request, SESSION_ID);
  const asked = state.messages.flatMap((message) => message.parts).find((part) => part.type === "tool-call" && part.toolCallId === CALL_ID);
  expect(asked).toMatchObject({ status: "cancelled", awaitingUser: false });
  expect(await decisions(request)).toEqual([]);
  // 提问所在的回合已结束、过程收起；展开后那一行标着「未作答」。
  const turn = page.locator('[data-role="assistant"]').first();
  const head = turn.locator('[data-slot="process-head"]');
  if (await head.count()) await head.click();
  const row = page.locator(`[data-tool-call-id="${CALL_ID}"]`).filter({ has: page.locator('[data-slot="call-row"]') });
  await expect(row).toContainText("未作答");
});

test("挂着提问点停止：卡片收敛成一行「已停止」，不再能作答", async ({ page, request }) => {
  const card = await openQuestion(page, request);
  // 输入框为空：唯一的按钮是停止。
  await expect(page.getByRole("button", { name: "发送", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "停止", exact: true }).click();

  await expect(card).toHaveCount(0);
  const state = await readState(request, SESSION_ID);
  const asked = state.messages.flatMap((message) => message.parts).find((part) => part.type === "tool-call" && part.toolCallId === CALL_ID);
  expect(asked).toMatchObject({ status: "cancelled", awaitingUser: false });
  const row = page.locator(`[data-tool-call-id="${CALL_ID}"]`).filter({ has: page.locator('[data-slot="call-row"]') });
  await expect(row).toContainText("已停止");
  expect(await decisions(request)).toEqual([]);
});

test("输入框只有一个按钮位：空着是停止，打字变发送；页头在等你操作时不再显示「等待结果」", async ({ page, request }) => {
  await openQuestion(page, request);
  const stop = page.getByRole("button", { name: "停止", exact: true });
  const send = page.getByRole("button", { name: "发送", exact: true });
  await expect(stop).toBeVisible();
  await expect(send).toHaveCount(0);

  await page.getByLabel("消息输入").fill("先说点别的");
  await expect(send).toBeVisible();
  await expect(stop).toHaveCount(0);

  await expect(page.getByText("等你操作").first()).toBeVisible();
  await expect(page.getByText("等待结果", { exact: true })).toHaveCount(0);
});
