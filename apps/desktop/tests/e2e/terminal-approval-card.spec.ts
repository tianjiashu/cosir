import { expect, test, type APIRequestContext } from "@playwright/test";

import type { TerminalApproval, TransportState } from "../../lib/assistant/contract";
import { seedState } from "./transport-fixtures";
import { API_ORIGIN as origin, OUTPUT_DIR } from "./e2e-env";

/**
 * 终端批准卡：与提问卡同一套「等你操作」外框，意图作标题、命令照原样、风险摘要在底栏；复制悬停才出现；
 * 来源是子 Agent 时页头带来源名（未起名走统一兜底）；已失效（409）时不报错。模拟后端与真实后端一致：
 * 决定后批准从页面移除，已不在页面上的批准 409 `TERMINAL_APPROVAL_EXPIRED`。
 */

const SESSION_ID = 42;
const OWN: TerminalApproval = {
  id: "own", originSessionId: SESSION_ID, originTitle: "审批", toolCallId: "call-own",
  command: "npm install && npm test -- --reporter=dot", shell: "/bin/zsh",
  cwd: "/Users/demo/projects/cosir/apps/desktop", intent: "安装依赖并运行测试",
};
const CHILD: TerminalApproval = {
  id: "child", originSessionId: 77, originTitle: "", toolCallId: "call-child",
  command: "npx playwright install chromium", shell: "/bin/zsh", cwd: "/Users/demo/projects/cosir", intent: "",
};

function state(approvals: TerminalApproval[]): TransportState {
  return {
    title: "审批",
    messages: [
      { id: "u-1", role: "user", turn: 1, inherited: false, source: null, outcome: null,
        parts: [{ type: "text", text: "装一下依赖然后跑测试", status: "completed" }] },
    ],
    messageOffset: 0,
    activity: { modelActivity: null, turn: 1, resumable: false, status: "suspended", endReason: null, error: null, usage: null,
      contextWindowTotal: null, launch: { modelConfigId: "demo/demo-model", reasoningEffort: null }, startedAt: "2026-10-27T00:00:00.000Z" },
    queued_messages: [],
    terminal_sessions: [], background_tasks: [], foreground_waits: [], deliverables: [], terminal_permission_mode: "request_approval",
    terminal_approvals: approvals,
  };
}

async function approvalBodies(request: APIRequestContext) {
  const telemetry = await (await request.get(`${origin}/__test__/telemetry`)).json() as { requestBodies: Array<Record<string, unknown>> };
  return telemetry.requestBodies.filter((body) => body.approvalId !== undefined);
}

test.beforeEach(async ({ page, request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId: SESSION_ID, title: "审批" } });
  await page.addInitScript(() => window.localStorage.clear());
});

test("批准卡：意图作标题、命令原样、风险摘要在底栏，复制悬停才出现，允许后卡片消失", async ({ page, request }) => {
  await seedState(request, SESSION_ID, state([OWN, CHILD]));
  await page.goto(`/sessions/${SESSION_ID}`);
  const cards = page.getByRole("region", { name: "终端执行批准" });
  await expect(cards).toHaveCount(2);
  const own = cards.first();

  // 与提问卡同一外框；本会话的请求页头不带来源。
  await expect(own).toHaveAttribute("data-slot", "awaiting-card");
  await expect(own.getByText("安装依赖并运行测试", { exact: true })).toBeVisible();
  await expect(own.getByRole("button", { name: /打开来源会话/ })).toHaveCount(0);
  await expect(own.locator("pre")).toHaveText(OWN.command);
  await expect(own.getByText("升级运行：可联网、可写工作区外 · 仅此一次")).toBeVisible();
  // 完整说明只在提示里，不常驻占版面。
  await expect(own.getByText(/可写你有权限的任何位置/)).toHaveCount(0);

  // 复制悬停命令块才出现。
  const copy = own.getByRole("button", { name: "复制命令" });
  await expect(copy).toHaveCSS("opacity", "0");
  await own.locator("pre").hover();
  await expect(copy).toHaveCSS("opacity", "1");

  // 子 Agent 的请求：页头带来源名，未起名走统一兜底；没有意图时标题兜底。
  const child = cards.nth(1);
  await expect(child.getByRole("button", { name: "打开来源会话：新会话" })).toBeVisible();
  await expect(child.getByText("运行一条命令", { exact: true })).toBeVisible();

  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await own.screenshot({ path: `${OUTPUT_DIR}/terminal-approval-card-${scheme}.png`, animations: "disabled" });
  }

  await own.getByRole("button", { name: "允许一次" }).click();
  await expect(cards).toHaveCount(1);
  expect(await approvalBodies(request)).toEqual([{ decision: "approve", sessionId: SESSION_ID, approvalId: "own" }]);
});

test("批准已失效（409）：不报错，按钮恢复可点", async ({ page, request }) => {
  await seedState(request, SESSION_ID, state([OWN]));
  await page.goto(`/sessions/${SESSION_ID}`);
  const card = page.getByRole("region", { name: "终端执行批准" });
  await expect(card).toBeVisible();
  // 模拟页面上的批准已被撤销而推送还没到：后端已不认这条请求。
  await page.route(`**/sessions/${SESSION_ID}/terminal-approvals/own/decision`, (route) =>
    route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ error: { code: "TERMINAL_APPROVAL_EXPIRED", message: "这次终端批准已失效", retryable: false } }) }));
  await card.getByRole("button", { name: "拒绝" }).click();
  await expect(card.getByRole("button", { name: "拒绝" })).toBeEnabled();
  await expect(page.getByText("无法提交终端批准")).toHaveCount(0);
});

test("决定请求一直不返回：超时后按结果未知结算，按钮恢复可点、可以重试", async ({ page, request }) => {
  await seedState(request, SESSION_ID, state([OWN]));
  await page.goto(`/sessions/${SESSION_ID}`);
  await page.clock.install();
  // 后端收到请求却一直不回：槽位命令必须有时限，否则按钮永远锁住。
  await page.route(`**/sessions/${SESSION_ID}/terminal-approvals/own/decision`, () => {});
  const card = page.getByRole("region", { name: "终端执行批准" });
  await card.getByRole("button", { name: "拒绝" }).click();
  await expect(card.getByRole("button", { name: "提交中…" })).toBeDisabled();
  await page.clock.fastForward(16_000);
  await expect(card.getByRole("button", { name: "拒绝" })).toBeEnabled();
  await expect(card.getByRole("button", { name: "允许一次" })).toBeEnabled();
});
