import { expect, test, type APIRequestContext } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { activity, assistantMessage, toolCallPart, transportState, userMessage } from "../unit/transport-fixtures";
import type { TransportMessagePart, TransportState } from "../../lib/assistant/contract";

/**
 * 活动行说「此刻在做什么」并计这个状态的时长：思考计这个模型步，执行计那次调用，换状态从头算；起点只认后端
 * 事实，所以刷新后接得上。潜在缺陷：执行时仍说「思考中」、计时跨状态累计、刷新归零、仍有三个点的动画。
 */
const seed = (request: APIRequestContext, state: TransportState) =>
  request.post(`${API_ORIGIN}/__test__/seed-state`, { data: { sessionId: 42, state, hold: true } });
const ago = (seconds: number) => new Date(Date.now() - seconds * 1000).toISOString();
const turn = (parts: TransportMessagePart[], model: { stepId: string; startedAt: string } | null) => transportState({
  messages: [userMessage("u", 1, "活动行计时"), assistantMessage("a", 1, [{ type: "text", text: "先看看配置。" }, ...parts])],
  activity: activity(1, "running", { modelActivity: model && { ...model, phase: "reasoning" } }),
});

test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-session`, { data: { sessionId: 42, title: "活动行计时" } });
});

test("the activity row names the current state and times only that state", async ({ page, request }) => {
  await seed(request, turn([], { stepId: "s0", startedAt: ago(0) }));
  await page.goto("/sessions/42");
  const head = page.locator('[data-slot="activity-group"]').last();
  const label = head.locator('[data-slot="current-activity-label"]');
  const elapsed = head.locator('[data-slot="call-elapsed"]');
  await expect(label).toHaveText("思考中");
  // 起点相对墙钟：页面就绪后再写，冷启动的编译时间不混进断言。
  await seed(request, turn([], { stepId: "s1", startedAt: ago(5) }));
  // 思考：扫光、没有三个点，计时从这个模型步开始。
  await expect(label).toHaveText("思考中");
  await expect(label).toHaveAttribute("data-thinking-active", "true");
  await expect(page.locator('[data-slot="activity-dots"]')).toHaveCount(0);
  await expect(elapsed).toHaveText(/^[5-7]秒$/);

  // 执行：说调用意图，计时换成这次调用的起点。
  const call = toolCallPart("read", "read_file", { status: "running", intent: "核对登录配置", args: { path: "config.ts" }, startedAt: ago(4) });
  await seed(request, turn([call], null));
  await expect(label).toHaveText("核对登录配置");
  await expect(elapsed).toHaveText(/^[4-6]秒$/);

  // 没有意图时退回动词；还在写参数（pending、没有起点）时不计时。
  await seed(request, turn([{ ...call, intent: undefined, status: "pending", startedAt: null }], null));
  await expect(label).toHaveText("读取文件");
  await expect(elapsed).toHaveCount(0);

  // 下一个模型步：回到「思考中」，从头算，不接调用或上一次思考。
  await seed(request, turn([{ ...call, status: "completed", startedAt: null }], { stepId: "s2", startedAt: ago(0) }));
  await expect(label).toHaveText("思考中");
  await expect(elapsed).toHaveCount(0);
  await expect(elapsed).toHaveText("3秒", { timeout: 5_000 });

  // 刷新：起点是后端事实，计时接着算而不是归零。
  await seed(request, turn([{ ...call, status: "completed", startedAt: null }], { stepId: "s2", startedAt: ago(40) }));
  await page.reload();
  await expect(label).toHaveText("思考中");
  await expect(elapsed).toHaveText(/^4[0-2]秒$/);
  await page.screenshot({ path: test.info().outputPath("activity-thinking-after-reload.png") });

  // 挂起：静止，不计时。
  const suspended = turn([{ ...call, status: "completed", startedAt: null }], null); suspended.activity!.status = "suspended";
  await seed(request, suspended);
  await expect(label).toHaveText("等待继续");
  await expect(label).toHaveAttribute("data-thinking-active", "false");
  await expect(elapsed).toHaveCount(0);
});

test("the first thinking before any reply is timed too", async ({ page, request }) => {
  const state = turn([], { stepId: "s1", startedAt: ago(12) }); state.messages.pop();
  await page.goto("/sessions/42");
  await seed(request, state);
  const row = page.locator('[data-slot="current-activity"]').locator("..");
  await expect(page.locator('[data-slot="current-activity-label"]')).toHaveText("思考中");
  await expect(row.locator('[data-slot="call-elapsed"]')).toHaveText(/^1[2-4]秒$/);
});
