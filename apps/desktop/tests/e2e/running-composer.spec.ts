import { installFakeDesktopHost } from "./fake-desktop-host";
import { expect, test, type APIRequestContext, type Page, type Request } from "@playwright/test";

import type { TransportState } from "../../lib/assistant/contract";
import { seedState } from "./transport-fixtures";
import { API_ORIGIN as origin } from "./e2e-env";

/**
 * 运行中的提交开关：活动 Run 期间照常能发送新消息——回车与按钮是插话（`queue: false`），⌘回车（其他平台
 * Ctrl+Enter）加入队列；插话消息是对话流末尾的气泡，排队消息在输入框上方的排队条里，两者都可立即
 * 发送、编辑（放回输入框）与删除。
 *
 * assistant-ui 的 React 层在 `thread.isRunning && !capabilities.queue` 时会禁用发送；本应用的排队在
 * 后端，能否提交由应用自己判定（`useComposerSubmit`）。活动用 `suspended`：它同样是活动态，模拟
 * 后端不会自行续跑，页面能稳定停在「运行中」。
 */

const SESSION_ID = 42;
// 没有装宿主时平台按浏览器 UA 推断，与跑测试的机器一致（见 composer-behavior.spec.ts）。
const QUEUE_KEY = process.platform === "darwin" ? "Meta+Enter" : "Control+Enter";
// 活动 Run 冻结的发起配置：运行中发送沿用它，而不是 composer 里的选择。
const RUN_LAUNCH = { modelConfigId: "demo/demo-model", reasoningEffort: null };

function activeState(queued: TransportState["queued_messages"] = []): TransportState {
  return {
    title: "测试对话",
    messages: [
      { id: "u-1", role: "user", turn: 1, inherited: false, clientMessageId: null, source: null, outcome: null,
        parts: [{ type: "text", text: "第一个问题", status: "completed" }] },
      { id: "assistant-turn-1", role: "assistant", turn: 1, inherited: false, clientMessageId: null, source: null, outcome: null,
        parts: [{ type: "text", text: "正在处理", status: "running" }] },
    ],
    messageOffset: 0, activity: {modelActivity: null,
      turn: 1, resumable: false, status: "suspended", endReason: null, error: null, usage: null,
      contextWindowTotal: null, launch: RUN_LAUNCH, startedAt: "2026-10-27T00:00:00.000Z",
    },
    queued_messages: queued,
    terminal_sessions: [], background_tasks: [], foreground_waits: [], terminal_approvals: [], deliverables: [], terminal_permission_mode: "request_approval" as const,
  };
}

async function openActiveSession(page: Page, request: APIRequestContext, state: TransportState) {
  await seedState(request, SESSION_ID, state);
  await page.goto(`/sessions/${SESSION_ID}`);
  await expect(page.getByText("正在处理", { exact: true })).toBeVisible();
}

const isCommand = (path: RegExp, method: string) => (req: Request) => (
  req.method() === method && path.test(new URL(req.url()).pathname)
);

test.beforeEach(async ({ page, request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId: SESSION_ID, title: "运行中的会话" } });
  await page.addInitScript(() => window.localStorage.clear());
});

for (const via of ["button", "enter", "queue"] as const) {
  const label = { button: "按钮插话", enter: "回车插话", queue: "⌘回车加入队列" }[via];
  test(`运行中可以发送新消息（${label}），沿用冻结配置、带上排队意图`, async ({ page, request }) => {
    await openActiveSession(page, request, activeState());
    const input = page.getByLabel("消息输入");
    await expect(input).toHaveAttribute("aria-placeholder", /加入队列$/);
    await input.fill("运行中追加的消息");

    const sent = page.waitForRequest(isCommand(/^\/sessions\/42\/messages$/, "POST"));
    if (via === "button") await page.getByRole("button", { name: "发送", exact: true }).click();
    else await input.press(via === "enter" ? "Enter" : QUEUE_KEY);

    const body = (await sent).postDataJSON();
    expect(body).toMatchObject({
      message: { parts: [{ type: "text", text: "运行中追加的消息" }] },
      modelConfigId: "demo/demo-model",
      queue: via === "queue",
    });
  });
}

test("插话消息是对话流末尾的气泡，可立即发送；排队消息在输入框上方的排队条里", async ({ page, request }) => {
  await openActiveSession(page, request, activeState([
    { id: "i-1", role: "user", delivery: "immediate", clientMessageId: null,
      parts: [{ type: "text", text: "顺便看下这个", status: "completed" }] },
    { id: "q-1", role: "user", delivery: "next_run", clientMessageId: null,
      parts: [{ type: "text", text: "之后再做", status: "completed" }] },
  ]));
  const queue = page.getByRole("region", { name: "排队消息" });
  await expect(queue.locator('[data-role="user"]')).toHaveCount(1);
  // 插话不在排队条里；排队消息只在排队条里出现一次。
  await expect(queue.getByText("顺便看下这个")).toHaveCount(0);
  await expect(page.getByText("之后再做", { exact: true })).toHaveCount(1);

  const interjection = page.locator('[data-role="user"]').filter({ hasText: "顺便看下这个" });
  const sendNow = page.waitForRequest(isCommand(/^\/sessions\/42\/queued-messages\/i-1\/send-now$/, "POST"));
  await interjection.getByRole("button", { name: "立即发送", exact: true }).click();
  await sendNow;
});

test("停止后留下的插话仍是气泡，可编辑放回输入框或删除", async ({ page, request }) => {
  // 插话投递后、模型读到之前 Run 被停止：不自动接续，它留在队列里；模型被删时它也开不了 Run，出口是改或删。
  const state = activeState([
    { id: "i-1", role: "user", delivery: "immediate", clientMessageId: null,
      parts: [{ type: "text", text: "停止前的插话", status: "completed" }] },
    { id: "i-2", role: "user", delivery: "immediate", clientMessageId: null,
      parts: [{ type: "text", text: "另一条插话", status: "completed" }] },
  ]);
  state.activity = { ...state.activity!, status: "cancelled", endReason: "user_cancelled" };
  state.messages[1].parts = [{ type: "text", text: "正在处理", status: "completed" }];
  await openActiveSession(page, request, state);
  await expect(page.getByRole("region", { name: "排队消息" })).toHaveCount(0);

  const first = page.locator('[data-role="user"]').filter({ hasText: "停止前的插话" });
  const withdrawn = page.waitForRequest(isCommand(/^\/sessions\/42\/queued-messages\/i-1$/, "DELETE"));
  await first.getByRole("button", { name: "编辑", exact: true }).click();
  await withdrawn;
  await expect(page.getByLabel("消息输入")).toHaveText("停止前的插话");

  const second = page.locator('[data-role="user"]').filter({ hasText: "另一条插话" });
  const deleted = page.waitForRequest(isCommand(/^\/sessions\/42\/queued-messages\/i-2$/, "DELETE"));
  await second.getByRole("button", { name: "删除", exact: true }).click();
  await deleted;
  await expect(page.getByText("另一条插话", { exact: true })).toHaveCount(0);
});

test("排队条：一行一条，可立即发送与删除", async ({ page, request }) => {
  await openActiveSession(page, request, activeState([
    { id: "q-1", role: "user", delivery: "next_run", clientMessageId: null,
      parts: [{ type: "text", text: "第一条排队", status: "completed" }] },
    { id: "q-2", role: "user", delivery: "next_run", clientMessageId: null,
      parts: [{ type: "text", text: "第二条排队", status: "completed" }] },
  ]));
  const queue = page.getByRole("region", { name: "排队消息" });
  await expect(queue.locator('[data-role="user"]')).toHaveCount(2);
  await expect(page.getByText("第一条排队", { exact: true })).toHaveCount(1);

  const first = queue.locator('[data-role="user"]').filter({ hasText: "第一条排队" });
  const sendNow = page.waitForRequest(isCommand(/^\/sessions\/42\/queued-messages\/q-1\/send-now$/, "POST"));
  await first.getByRole("button", { name: "立即发送", exact: true }).click();
  await sendNow;
  // 立即发送后转为插话：离开排队条，成为对话流末尾带「立即发送」的气泡。
  await expect(queue.getByText("第一条排队")).toHaveCount(0);
  await expect(page.locator('[data-role="user"]').filter({ hasText: "第一条排队" })
    .getByRole("button", { name: "立即发送", exact: true })).toBeVisible();

  const second = queue.locator('[data-role="user"]').filter({ hasText: "第二条排队" });
  const deleted = page.waitForRequest(isCommand(/^\/sessions\/42\/queued-messages\/q-2$/, "DELETE"));
  await second.getByRole("button", { name: "删除", exact: true }).click();
  await deleted;
  await expect(page.getByText("第二条排队", { exact: true })).toHaveCount(0);
  await expect(queue).toHaveCount(0);
});

test("编辑排队消息：撤回这一条，正文放回输入框并排在已有草稿之前", async ({ page, request }) => {
  await openActiveSession(page, request, activeState([
    { id: "q-1", role: "user", delivery: "next_run", clientMessageId: null,
      parts: [{ type: "text", text: "排队中的消息", status: "completed" }] },
  ]));
  const input = page.getByLabel("消息输入");
  await input.fill("还没发的草稿");
  const queue = page.getByRole("region", { name: "排队消息" });
  const queued = queue.locator('[data-role="user"]').filter({ hasText: "排队中的消息" });
  await queued.hover();

  const withdrawn = page.waitForRequest(isCommand(/^\/sessions\/42\/queued-messages\/q-1$/, "DELETE"));
  await queued.getByRole("button", { name: "编辑", exact: true }).click();
  await withdrawn;

  // 条目离开队列，内容回到输入框；不再有原地编辑框。
  await expect(queue).toHaveCount(0);
  // 编辑器按段落渲染，textContent 不含段间换行。
  await expect(input).toHaveText(/排队中的消息\s*还没发的草稿/);
});

test("迟到的发送失败并入当前草稿，不覆盖请求期间的新输入", async ({ page, request }) => {
  await openActiveSession(page, request, activeState());
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/sessions/42/messages", async route => {
    await gate;
    await route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: { error: { code: "TEST_REJECTED", message: "测试拒绝" } } }) });
  });
  const input = page.getByLabel("消息输入");
  await input.fill("请求失败的内容");
  const sent = page.waitForRequest(isCommand(/^\/sessions\/42\/messages$/, "POST"));
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await sent;
  await input.fill("用户新写的内容");
  release();
  await expect(page.getByRole("dialog")).toContainText("发送失败");
  await page.getByRole("dialog").getByRole("button", { name: "知道了" }).click();
  await expect(input).toHaveText(/请求失败的内容\s*用户新写的内容/);
});

test("撤回完成前切换 Session，内容保存在原 Session 草稿中", async ({ page, request }) => {
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId: 43, title: "另一个Session" } });
  await openActiveSession(page, request, activeState([{ id: "q-late", role: "user", delivery: "next_run", clientMessageId: null, parts: [{ type: "text", text: "迟到撤回内容" }] }]));
  const input = page.getByLabel("消息输入");
  await input.fill("原Session草稿");
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/sessions/42/queued-messages/q-late", async route => {
    await gate;
    await route.continue();
  });
  const sent = page.waitForRequest(isCommand(/^\/sessions\/42\/queued-messages\/q-late$/, "DELETE"));
  await page.getByRole("region", { name: "排队消息" }).getByRole("button", { name: "编辑", exact: true }).click();
  await sent;
  await page.getByRole("treeitem").filter({ hasText: "另一个Session" }).click();
  await expect(page).toHaveURL(/\/sessions\/43$/);
  await input.fill("另一个Session草稿");
  const response = page.waitForResponse(res => res.url().endsWith("/queued-messages/q-late") && res.request().method() === "DELETE");
  release();
  await response;
  await expect(input).toHaveText("另一个Session草稿");
  await page.getByRole("treeitem").filter({ hasText: "运行中的会话" }).click();
  await expect(page).toHaveURL(/\/sessions\/42$/);
  await expect(input).toHaveText(/迟到撤回内容\s*原Session草稿/);
});


test("后端暂停时提交恢复草稿并提示，不发送业务请求", async ({ page, request }) => {
  await installFakeDesktopHost(page);
  await page.addInitScript(() => {
    const listeners = new Set<Parameters<NonNullable<typeof window.cosirHost>["backend"]["onChange"]>[0]>();
    window.cosirHost!.backend.onChange = listener => { listeners.add(listener); return () => { listeners.delete(listener); }; };
    Object.assign(window, { pauseTestBackend: () => {
      for (const listener of listeners) listener({ generation: 2, status: { state: "stopped" }, backendBaseUrl: null });
    } });
  });
  await openActiveSession(page, request, activeState());
  const input = page.getByLabel("消息输入");
  await input.fill("后端暂停仍需保留的草稿");
  let sent = 0;
  page.on("request", req => { if (isCommand(/^\/sessions\/42\/messages$/, "POST")(req)) sent++; });
  await page.evaluate(() => (window as unknown as { pauseTestBackend(): void }).pauseTestBackend());
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await input.press("Enter");
  await expect(page.getByRole("dialog")).toContainText("本机后端暂不可用");
  await page.getByRole("dialog").getByRole("button", { name: "知道了" }).click();
  await expect(input).toHaveText("后端暂停仍需保留的草稿");
  expect(sent).toBe(0);
});
