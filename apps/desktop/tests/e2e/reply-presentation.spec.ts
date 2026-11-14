import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import type { TransportMessagePart, TransportState } from "../../lib/assistant/contract";
import { completedState, seedState } from "./transport-fixtures";
import { fakeBrowserCalls, installFakeDesktopHost } from "./fake-desktop-host";
import { API_ORIGIN as origin } from "./e2e-env";

/**
 * 回复呈现（`present_files` / `offer_next_step`）：交付物卡片列出 Agent 声明的成果文件；下一步建议只挂在会话
 * 最后一条正常完成的回复上，点按钮等于替用户发出标签原文。
 */

const SESSION_ID = 42;
const LAUNCH = { modelConfigId: "demo/demo-model", reasoningEffort: null };

const FILES: TransportMessagePart = {
  type: "tool-call", toolCallId: "call-files", toolName: "present_files", presentation: "quiet",
  args: { files: [{ path: "docs/report.md", caption: "结论在第 2 节" }, { path: "/tmp/outside/chart.png", caption: null }] },
  intent: null, status: "completed", error: null, awaitingUser: false,
  display_data: { kind: "deliverables", files: [
    { path: "docs/report.md", absolute_path: "/workspace/docs/report.md", caption: "结论在第 2 节" },
    { path: "/tmp/outside/chart.png", absolute_path: "/tmp/outside/chart.png", caption: null },
  ] },
};
const OFFER: TransportMessagePart = {
  type: "tool-call", toolCallId: "call-offer", toolName: "offer_next_step", presentation: "quiet",
  args: { ask: "要把同样的修复应用到移动端吗？", replies: ["好，应用", "先不用"] },
  intent: null, status: "completed", error: null, awaitingUser: false,
  display_data: { kind: "next-step", ask: "要把同样的修复应用到移动端吗？", replies: ["好，应用", "先不用"] },
};

function deliveredState(): TransportState {
  const state = completedState(1, { id: "u-1", parts: [{ type: "text", text: "写一份报告", status: "completed" }] }, [
    FILES, OFFER, { type: "text", text: "报告写好了。", status: "completed", finalReply: true },
  ]);
  return { ...state, activity: { ...state.activity!, launch: LAUNCH } };
}

async function open(page: Page, request: APIRequestContext, state: TransportState) {
  await seedState(request, SESSION_ID, state);
  await page.goto(`/sessions/${SESSION_ID}`);
  await expect(page.getByText("报告写好了。")).toBeVisible();
}

async function sentTexts(request: APIRequestContext): Promise<string[]> {
  const telemetry = await (await request.get(`${origin}/__test__/telemetry`)).json() as {
    requestBodies: Array<{ message?: { parts?: Array<{ type: string; text?: string }> } }>;
  };
  return telemetry.requestBodies.flatMap((body) => body.message?.parts?.flatMap((part) => (part.type === "text" && part.text ? [part.text] : [])) ?? []);
}

test.beforeEach(async ({ page, request }) => {
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId: SESSION_ID, title: "回复呈现" } });
  await page.addInitScript(() => window.localStorage.clear());
});

test("交付物卡片列出声明的文件，可在文件夹中显示；两个工具都不在过程里留调用行", async ({ page, request }) => {
  await installFakeDesktopHost(page);
  await open(page, request, deliveredState());

  const card = page.locator('[data-slot="deliverables-card"]');
  await expect(card.getByRole("listitem")).toHaveCount(2);
  await expect(card).toContainText("report.md");
  await expect(card).toContainText("结论在第 2 节");
  // 没有说明的文件显示路径。
  await expect(card).toContainText("/tmp/outside/chart.png");
  // 工作区外的非网页文件没有可打开的界面，只能在文件夹中显示。
  await expect(card.getByRole("button", { name: "打开 chart.png" })).toHaveCount(0);
  await expect(page.locator('[data-slot="process-head"]')).toHaveCount(0);

  await card.getByRole("button", { name: "在文件夹中显示 chart.png" }).click();
  await expect.poll(async () => (await fakeBrowserCalls(page)).filter((call) => call.method === "files.reveal"))
    .toEqual([{ method: "files.reveal", args: ["/tmp/outside/chart.png"] }]);
});

test("点下一步建议的按钮就是替用户发出标签原文，发出后建议消失", async ({ page, request }) => {
  await open(page, request, deliveredState());

  const offer = page.locator('[data-slot="next-step-card"]');
  await expect(offer).toContainText("要把同样的修复应用到移动端吗？");
  await expect(offer.getByRole("button")).toHaveText(["好，应用", "先不用"]);

  await offer.getByRole("button", { name: "好，应用" }).click();
  await expect.poll(() => sentTexts(request)).toEqual(["好，应用"]);
  await expect(offer).toHaveCount(0);
});

test("建议只挂在最后一条回复上：之后又有回合时不再显示，交付物卡片仍在", async ({ page, request }) => {
  const first = deliveredState();
  const second = completedState(2, { id: "u-2", parts: [{ type: "text", text: "再看看别的", status: "completed" }] }, [
    { type: "text", text: "看过了。", status: "completed", finalReply: true },
  ]);
  // 结局属于 Run 最后一个回合的回复；这里两个回合各是一个已完成的 Run。
  await open(page, request, { ...second, messages: [...first.messages, ...second.messages] });

  await expect(page.getByText("看过了。")).toBeVisible();
  await expect(page.locator('[data-slot="deliverables-card"]')).toBeVisible();
  await expect(page.locator('[data-slot="next-step-card"]')).toHaveCount(0);
});
